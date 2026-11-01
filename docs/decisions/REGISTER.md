# Decision register

**Status:** current. **Reviewed:** 2026-10-14 (documentation audit; entries were read against code names, tests and specs, not re-run). **Updated:** 2026-11-02 (W-31).
**What this is:** every owner and architecture decision that is in force today, one row each. A decision that was reversed or replaced is not here; it is in [../history/RULINGS.md](../history/RULINGS.md).
**What this is not:** a place for detail. The "Where" column names the document or test that holds the detail.

## How to read it

| Status | Meaning |
|---|---|
| **Implemented** | Decided and built. Evidence is the named test or spec. Rows carried over from earlier records were not re-traced on 2026-10-14. |
| **Decided, pending** | The owner has ruled; the code, configuration or deployment does not match yet. The row is also listed in [Pending changes](#pending-changes). Never describe these as working. |
| **Proposed** | A design the owner wants validated. Not approved for adoption. |

Precedence when two entries disagree: the later dated entry wins; an owner statement beats an inferred rule ([../engineering/documentation-policy.md](../engineering/documentation-policy.md)). Unanswered questions are in [OPEN.md](OPEN.md); an unanswered question is never treated as approved.

---

## Owner decisions of 2026-10-14

These replace earlier rulings where noted. All are **Decided, pending** unless marked.

| ID | Decision | Replaces | Where |
|---|---|---|---|
| J-10 | **Job approval flow.** Recruiter creates the job, prepares the JD and assigns a Hiring Manager. Hiring Manager reviews the JD and prepares Skills and the SWOT, then submits. HR or Recruitment Manager approves, or returns it. Recruiter publishes only an approved job. "Skills Matrix" in the owner wording means the Skills step (no matrix, no weights). | CONTRACT v10 publish rule (JD + saved skills only); the 2026-09-25 deletion of the approval chain; RBAC spec 9.6 and 17 (no HR approval step) | [../spec/JOB_SETUP_FLOW.md](../spec/JOB_SETUP_FLOW.md) |
| J-11 | **Who may do what.** Super Admin: full authority, overrides audited, but follows the same steps (cannot skip approval). HR or Recruitment Manager: create jobs, edit JD, Skills and SWOT, assign Hiring Managers, approve; **cannot publish**. Recruiter: create jobs, edit assigned draft JDs, publish assigned approved jobs; **cannot edit Skills or SWOT, cannot approve**. Hiring Manager: edit assigned JD, Skills, SWOT; cannot publish or approve. Interview Manager: no job preparation or publishing. | RBAC spec matrix cell "Publish job: HR Manager YES*" | [../spec/RBAC_SPECIFICATION.md](../spec/RBAC_SPECIFICATION.md), [../reference/RBAC.md](../reference/RBAC.md) |
| J-12 | **Publish needs** a completed JD, saved Skills and HR approval of the exact revision being published. The SWOT is not a publication blocker. | v10: publish needs JD + skills only | [../spec/JOB_SETUP_FLOW.md](../spec/JOB_SETUP_FLOW.md) |
| J-13 | **Approval validity.** A material change to the JD, hiring requirements, Skills, compensation or hiring-relevant SWOT invalidates the pre-publication approval and needs fresh HR review before publishing. Cosmetic edits do not (internal notes and formatting). The exact field list is not written (OPEN T-08). Approval history is kept, every change is recorded, and publication uses the exact approved revision. | none (new) | same |
| J-14 | **Independent approval.** Both the HR Manager and the Recruitment Manager may approve (ruled 2026-10-14). Anyone who materially prepared the JD, Skills, SWOT or requirements cannot ordinarily approve that revision; another HR or Recruitment Manager must. The Super Admin may approve when no independent approver exists, including an audited self-approval exception for small companies. Record approver, justification, revision and audit history. Cosmetic edits do not count as preparation. No unauthorised role may ever approve. | none (new) | same |
| J-15 | **Return for corrections.** HR chooses the recipient: back to the Recruiter (JD problems) or to the Hiring Manager (Skills problems), with a written reason. | none (new) | same |
| J-16 | **SWOT timing.** The SWOT may be written and edited at any time before or after publishing and is never shown to candidates. It must be complete before assessment invitations are sent (J-20) and freezes when the first assessment begins (J-19). An edit after publication does not unpublish the job (J-18). | v10: "SWOT editable independently, also after the freeze" | [../spec/JOB_SETUP_FLOW.md](../spec/JOB_SETUP_FLOW.md) |
| S-20 | **Coding questions without a sandbox.** Candidate code is never executed and no sandbox (Judge0) is used. A **static checker** analyses the code without running it and AI reviews and evaluates it. Coding questions go only to coding (computing) roles. The old hidden-test scoring policy is not retained. Languages, tools, scoring proportions and candidate feedback are unresolved ([OPEN.md](OPEN.md) T-02). Confirmed 2026-10-14 (second ruling). | The 2026-09-25 sandbox design (hidden tests, Run button, 70/30 score, answer-key table) and the 2026-09-28 "on hold" ruling | [../spec/CODING_ASSESSMENT.md](../spec/CODING_ASSESSMENT.md) |
| S-21 | **Yukti five-part pre-assessment score, approved for adoption.** Must-have 45, Nice-to-have 15, Role Fit 20, Experience 15, Company Need 5 (percent). Compensation, notice period and document readiness stay separate from the suitability score. Evidence grounding, missing-data handling and assessment blending stay. The code still computes the six-part model (40/15/15/15/5/10, with application answers 10); the change is pending. Policy versioning and reproducibility of historical scores are unresolved (T-05). | The six-part weights as the target; the earlier "provisional, proposed" status | [../spec/RETRIEVAL.md](../spec/RETRIEVAL.md), `backend/app/services/yukti/config.py` |
| A-20 | **Retrieval-time prompt-injection screen is mandatory.** Retrieved resume and document content is screened before a model sees it. Suspicious content is **quarantined**, excluded from the AI context, and the incident is recorded; processing continues safely when possible. The screen exists in code (`services/safety/content.py`) but nothing calls it. | The earlier "not in force, owner to decide" note | [../engineering/security.md](../engineering/security.md) |
| I-20 | **Email.** Amazon SES only; no SMTP fallback. `noreply@readypick.ai` is kept for platform security emails (security codes). `contact@readypick.ai` is the sender for every other platform email and for fallback recruitment email. A company's own approved sender is preserved for candidate lifecycle mail, also through SES. Primary mailbox `manjunath@readypick.ai`; public alias and contact `contact@readypick.ai`. No other mailbox or alias may be invented. Every `@pickready.app` address is obsolete and needs replacing. SES identity verification and development and test mail handling are unresolved (T-03). | The 2026-09-06 "email transport is deployment data, smtp or ses" rule, the Gmail SMTP rule and the first I-20 text (all platform mail from `contact@`) | [../spec/CANDIDATE_COMMUNICATIONS.md](../spec/CANDIDATE_COMMUNICATIONS.md) |
| W-20 | **Graphify is optional.** Use it only for unfamiliar architecture, cross-module dependencies and complex relationships, when its graph exists and is current. No mandatory queries, no blocking hooks, no repeated context injection. The `PreToolUse` hooks in `.claude/settings.json` were removed on 2026-10-14 (owner approved); the untracked local `.codex/hooks.json` is pending. Status: **Implemented** for Claude Code; Codex hook file **pending**. | The "MANDATORY graphify query" instruction | [../engineering/agent-workflow.md](../engineering/agent-workflow.md) |
| W-21 | **Latest owner statement wins.** A newer owner instruction overrides older ones without re-asking. Questions to the owner explain current behaviour, the problem, a real example, each option's consequences and a recommendation. | none | [../engineering/documentation-policy.md](../engineering/documentation-policy.md) |
| W-22 | **Documentation-only tasks** change documents and agent instruction files only. Decisions are recorded, never implemented, and appear under Pending changes. | none | [../engineering/documentation-policy.md](../engineering/documentation-policy.md) |

### Second ruling of 2026-10-14

Answers to the follow-up questions. All **Decided, pending**: none is built. Open technical work that these rulings leave undecided is in [OPEN.md](OPEN.md) (T-01 to T-07).

| ID | Decision | Replaces | Where |
|---|---|---|---|
| J-17 | **Explicit job states:** Draft, Technical Review, HR Review, Approved, Published. A job can be sent back for correction and resubmitted. Technical Review is the Hiring Manager step of J-10. The exact database states, transitions and migration are unresolved (T-01). | The unnamed approval states of J-10 | [../spec/JOB_SETUP_FLOW.md](../spec/JOB_SETUP_FLOW.md) |
| J-18 | **SWOT changes after publication.** A material SWOT change after publication does not unpublish the job, but it needs HR review before it affects future assessments; assessments already started keep their snapshots. The SWOT freezes permanently when the first assessment begins. How the review gates future assessments is unresolved (OPEN T-06). | none (resolves the open question on a live job) | [../spec/JOB_SETUP_FLOW.md](../spec/JOB_SETUP_FLOW.md) |
| J-19 | **Two freeze moments.** The JD and Skills freeze at the first genuine application (J-03, unchanged). The SWOT freezes when the first assessment begins, with no SWOT change afterwards. | none (confirms J-03 and J-16) | same |
| J-20 | **The SWOT must be complete before assessment invitations are sent.** | The weaker J-16 wording "before assessment" | [../spec/HIRING_WORKFLOW.md](../spec/HIRING_WORKFLOW.md) |
| J-21 | **Assignment.** The Recruiter assigns the Hiring Manager during job creation. The HR Manager, the Recruitment Manager and the Company Super Admin may reassign. A Recruiter cannot reassign afterwards. | none | [../spec/JOB_SETUP_FLOW.md](../spec/JOB_SETUP_FLOW.md) |
| A-21 | **AI evaluation explanations are recruiter-facing only.** A candidate receives application status, not internal scoring explanations, subject to applicable legal obligations (which nobody has yet mapped). | The 2026-09-08 brief item for a candidate-facing explanation (designed, never built) | [../architecture/AI_RUNTIME.md](../architecture/AI_RUNTIME.md) |
| A-22 | **Production-grade security controls apply whenever real users are served,** whether the environment is called pilot or production. Today `ENVIRONMENT=pilot` makes `Settings.is_production` false, so five production guards are inert; that is a gap against this ruling (Pending change 7). | The silent assumption that "pilot" is exempt | [../engineering/security.md](../engineering/security.md) |
| C-10 | **Per-credit pricing replaces monthly subscriptions.** Existing subscribers keep their paid period and move to credits at their next renewal. Conversion rates, treatment of unused benefits, customer notification terms, credit price, GST, expiry, refunds and billing transitions are unresolved (OPEN T-04). **Implemented today:** four monthly Razorpay plans, a Starter-only top-up and the 2026-09-30 terms (archived in [../history/SUPERSEDED.md](../history/SUPERSEDED.md) F); keep that code working until the replacement ships (Pending change 8). | The 2026-09-30 monthly plans ruling (C-01) | [../product/PRD.md](../product/PRD.md) |

---

## Owner decision of 2026-10-30

The behavior is specified. As of 2026-10-30 the pilot task definition for the API (`readypick-pilot-api:56`) sets `DEMO_MODE=true` and `DEMO_MODE_CONFIRM=public-demo`. The frontend image (`readypick-pilot-frontend:46`, tag `sha-cdf9b73d358a`) was built with `NEXT_PUBLIC_DEMO_MODE=true`. tfvars were not changed and must not say `demo_mode=true`. The CAPTCHA removal (A-24, under Auth, security and privacy) is deployed on `readypick-pilot-api:56` and `readypick-pilot-frontend:46`.

| ID | Decision | Replaces | Where |
|---|---|---|---|
| A-23 | **Public demo mode on the existing codebase (2026-10-30).** Default off. Amends A-05 only while `DEMO_MODE` is on. A-24 removed the server-drawn CAPTCHA from every auth surface, including when the flag is off. On `ENVIRONMENT=production` (the live pilot sets this, even though the Terraform composition is named pilot), `DEMO_MODE` also requires `DEMO_MODE_CONFIRM=public-demo`; a missing confirm refuses to boot. Existing Firebase and Razorpay routes stay unchanged when the flag is off. Only these routes skip Firebase and Razorpay, and only when the flag is on: `POST /api/v1/auth/demo/session`, `POST /api/v1/company-onboarding/demo`, `POST /api/v1/billing/demo/purchase`, and the `public_demo` branch of invite setup-password. `GET /api/v1/auth/demo-status` reports the flag and does not mint a session. Demo login accepts any syntactically valid email and any nonempty password. It never binds a user who is not `public_demo`, never the owner, never `super_admin`, and never the permanent `is_demo` companies (Sarkar Corp, ACRM Corp, Specter and Co.). If any user with that email is not `public_demo`, the email is refused with 409 so the frontend can fall through to Firebase. Company login retrieves only. Company registration creates. Candidate and BD login create or retrieve per email and portal (A-25) and must not claim a sourced candidate (`email_verified` false). `public_demo` is a new column and is not `is_demo`. `is_demo` remains the C-07 billing-exemption flag. Public demo companies get a real ledger grant of `DEMO_INITIAL_CREDITS` (default 25), real deductions and normal credit gates. Registration writes a `subscription_charge` of amount 0 and does not call Razorpay. Credit packs call the real `settle_purchase` path with a demo order id and do not call Razorpay. Public demo tenants get 409 on real payment routes. Mail is never withheld and nothing expires a demo row (A-25). Same-email collision with a real user is 409. Demo users have no `firebase_uid`. The behavior is specified. As of 2026-10-30 the pilot task definition for the API (`readypick-pilot-api:56`) sets `DEMO_MODE=true` and `DEMO_MODE_CONFIRM=public-demo`. The frontend image (`readypick-pilot-frontend:46`, tag `sha-cdf9b73d358a`) was built with `NEXT_PUBLIC_DEMO_MODE=true`. tfvars were not changed and must not say `demo_mode=true`. The CAPTCHA removal (A-24) is deployed on `readypick-pilot-api:56` and `readypick-pilot-frontend:46`. | Amends A-05 only while `DEMO_MODE` is on | [../operations/DEMO_MODE.md](../operations/DEMO_MODE.md) |

---

## Owner pre-approval of 2026-11-01

The owner pre-approved every decision needed to complete the production defect mission. These are the decisions taken under it.

| ID | Decision | Where |
|---|---|---|
| W-30 | **Durable background work (2026-11-01).** The `background_jobs` ledger is the outbox and the status of record; Redis is a cache only. A run unknown to both reports UNKNOWN, never PENDING. A Lambda 202 is acceptance, not placement. Bounds: 120s queue grace, 300s and 900s start grace, 10 minute stale heartbeat, 8 re-drives, 24 hour capacity window, 30 day purge. Recording ECS STOPPED reasons is decided, pending (needs an EventBridge rule). | `workers/dispatch.py`, `jobs.py`, `status.py`, [BACKGROUND_WORK.md](../spec/BACKGROUND_WORK.md) |
| P-30 | **Sustained high sound level is an advisory, not Path B (2026-11-01).** `SUSTAINED_HIGH_SOUND_LEVEL` is Path C, never counts toward the three-warning termination, is excluded during a spoken answer, and is measured as uncalibrated microphone level. Escalation to Path B is not built and needs a separate owner decision. Phone thresholds (0.65, 3 frames) are unchanged pending measured staged data. | [PROCTORING.md](../spec/PROCTORING.md) |
| P-31 | **The proctoring report says why it is absent (2026-11-01).** States: no session, in progress, generating, failed, partially unavailable, no events. With no analysis service every report is partially unavailable. The reconciler re-dispatches ended sessions with no report (overdue 30 minutes, 7 day window, 200 per run). | `services/proctoring/report.py`, `prism_view.py` |
| D-30 | **Candidate verification-document upload is retired (2026-11-01).** `POST /bgv/me/documents` and the profile card are removed. List and withdraw stay. Nothing is purged without owner authority (Q-34). | [BGV_LEGACY_DOCUMENTS.md](../operations/BGV_LEGACY_DOCUMENTS.md) |
| V-30 | **Voice answers (2026-11-01).** Transcribe job names use the prefix `readypick-voice-` to fit the existing IAM grant rather than widening it. The API task definition must carry `TRANSCRIBE_ENABLED=true`. The PRISM PDF is fetched as a blob so a refusal shows its reason. The video status gains a stage word beside the four-word status. | `services/video/transcribe.py`, `ppi-report-modal.tsx`, `assessment_video_access.py` |
| W-31 | **Production defects release deployed to the pilot (2026-11-02).** Under the owner's pre-approval, PR #25 was merged and applied with the repository scripts, not a Terraform apply: migration `0138_background_jobs`, the three image Lambdas, the `readypick-assessment-trigger` zip, `readypick-pilot-api:58`, `readypick-pilot-frontend:48`, agent `:54`. The `readypick-sweep-background-jobs` schedule (`rate(5 minutes)`) was created by CLI and imported into the pilot state. A full pilot `terraform apply` stays forbidden until the live drift is reconciled (it would destroy the RDS sleep guard, recreate four interface endpoints, strip `DEMO_MODE` from the Lambdas and raise the frontend to 0.5 vCPU). The analysis service stays at desired 0, so `verify-deployment.sh` reports it as having no running tasks. | [PRODUCTION_DEFECTS_2026-11-01.md](../verification/PRODUCTION_DEFECTS_2026-11-01.md) section 7 |

---

## Product and naming

| ID | Decision | Status | Where |
|---|---|---|---|
| P-01 | The product is **Vivekium** (Varpitech LLP). The domain `readypick.ai` stays. Deliberate `pickready` and `readypick` identifiers (environment variable names, `pickready.*` task names, infrastructure names, database roles, migrations) are not renamed. | Implemented | [../../CLAUDE.md](../../CLAUDE.md) |
| P-02 | **No number ever reaches a client, with no exception.** Scores are internal; words are made server-side. `match_percent` is deleted and `harness/probes.SANCTIONED_NUMERIC_FIELDS` is empty. Operational counts and a countdown clock are not assessment signals. | Implemented | `test_platform_audit.py` |
| P-03 | **One rating scale, four grades**: Highly Matching, Matching, Moderately Matching, Not Matching. Cut points 90, 75, 60, inclusive upward. `services/rating.py` is the only definition. | Implemented | `test_grade_scale_consistency.py` |
| P-04 | **Tatva Assessment** is the process; **PRISM Report** is the document (subtitle "Evidence-Based Role Intelligence & Suitability Mapping"). The two names are never used for each other. | Implemented | `report_pdf.py` |
| P-05 | Recruiter-visible vocabulary is JD, SWOT, Skills, AI Match, Tatva Assessment, PRISM Report, Proctoring Report. Not PPI, matrix, framework, matching categories, AI Score or retake. Code identifiers keep their names. | Implemented | `test_user_facing_copy_names.py` |
| P-06 | PRISM section order is fixed: AI Match, Overall Assessment, Must-have, Nice-to-have, Behavioural, Gap Analysis and Action Plan, Validation, Proctoring. Three radar charts (Overall, Must-have, Nice-to-have), no number on any axis. Screen and PDF share one serializer. | Implemented | [../spec/ASSESSMENT_FLOW.md](../spec/ASSESSMENT_FLOW.md) |
| P-07 | A PRISM Report is **immutable**: insert-only at the database (trigger and revoked UPDATE). There is no retake. | Implemented | migration 0130 |
| P-08 | **Proctoring is mandatory**, no flag. A lost camera or microphone pauses the assessment (Path P) within set limits; the server decides every threshold. Session media is stored compressed in S3 and purged 90 days after the session or at the job-closure purge, whichever is first. | Implemented | [../spec/PROCTORING.md](../spec/PROCTORING.md) |
| P-09 | Candidate navigation is New Jobs, Applied Jobs, Updates, Messages, My Profile. | Implemented | `frontend/` |
| P-10 | Text is never grey; navy is structure, teal is evidence; the brand teal fails AA as body text on white, so teal text on white is `teal-700`. No gradient between two hues. | Implemented | [../../DESIGN.md](../../DESIGN.md) |
| P-11 | Customer role chain is Super Admin, Recruitment Manager, Recruiter, Hiring Manager. CEO, MD and Functional Head are leaf roles under the Super Admin (CEO and MD read everything and write only their own leadership input; a Functional Head is bound to one department). Permissions are data (`role_permissions` plus a per-user overlay), never a role branch. | Implemented | [../reference/RBAC.md](../reference/RBAC.md) |
| P-12 | **Leadership Intelligence** replaces Drishti. Versions are insert-only; the compiler is deterministic; an AI draft is never saved; Save Skills freezes the version the contract carries. | Implemented | `services/leadership/` |
| P-13 | The Company Profile is current company context; a job snapshots its narrative at creation. Gate 1: no Company Profile `about_company`, no job. | Implemented | [../spec/HIRING_WORKFLOW.md](../spec/HIRING_WORKFLOW.md) |
| P-14 | Reporting, EEO and impact-ratio surfaces are refused: no jurisdiction was named and the customers are Indian entities. Protected-attribute inference is refused outright. | Implemented | [../architecture/AI_RUNTIME.md](../architecture/AI_RUNTIME.md) |

## Commercial

| ID | Decision | Status | Where |
|---|---|---|---|
| C-01 | **Current billing implementation, superseded in intent by C-10:** four monthly plans (Starter, Growth, Scale, Pro) on Razorpay subscriptions, a paid 30-day Starter pilot, one credit per completed assessment, three-month rollover and a Starter-only top-up. The 2026-09-30 terms and prices are archived in [../history/RULINGS.md](../history/RULINGS.md). Keep this code working until the owner rules on the transition. | Implemented, to be replaced | `services/monthly_plans.py`, `services/credits.py`, `services/credit_packs.py`, migration 0136 |
| C-06 | The credit ledger is the only balance: `SUM(subunits_delta)`, 60 sub-units per credit, integer only, every write carries a unique idempotency key. Gates ask `services/entitlements.py`; zero credits never block login or reads. A completed assessment is charged even into the negative; the next invitation batch is what is blocked. | Implemented | [../engineering/backend.md](../engineering/backend.md) |
| C-07 | Demonstration tenants are exempt from billing refusals, never from billing records (`tenants.is_demo`). | Implemented | `services/entitlements.py` |

## Auth, security and privacy

| ID | Decision | Status | Where |
|---|---|---|---|
| A-01 | Identity is Firebase (Google, or email and password). Phone sign-in and every login OTP are removed. Database roles stay authoritative; Firebase is identity only. | Implemented | [../engineering/security.md](../engineering/security.md) |
| A-24 | **Server-drawn text CAPTCHA removed (2026-10-30).** The server-drawn text CAPTCHA is removed from every auth surface. No challenge is drawn and no proof is checked. Nothing pretends a proof was checked. The session-exchange field `captcha_purpose` stays and is only the portal filter (candidate, org, owner, bd). `captcha_proof` may still be sent by an old client and is not read. Rate limits, the six-digit security code, Firebase identity, authorization, and tenant isolation stay. This replaces A-02 (2026-09-29). The removal is deployed on `readypick-pilot-api:56` and `readypick-pilot-frontend:46`. | Implemented | `api/auth.py` (session exchange, password reset, password change), `api/company_onboarding.py` (company registration), `api/companies.py` (invite setup) |
| A-25 | **Demo users are persistent real users, and demo mail is real (2026-10-31, owner statement).** Amends A-23. While `DEMO_MODE` is on: (1) `POST /api/v1/auth/demo/session` accepts any syntactically valid email and any nonempty password for the candidate, company and BD portals. The same email and portal always resolves to the same user; candidate and BD create on first login, serialized by an advisory lock so parallel first logins make one row. Company sign-in retrieves a public-demo company and never creates one, because a company user cannot exist without a tenant: a new email registers through `POST /api/v1/company-onboarding/demo`. (2) The provider portal is never reachable through demo login and the owner email is refused; ownership is the Firebase owner session. A BD demo user is scoped to its own data: it lists, edits and exports only the BD leads it owns (a lead it does not own is 404), it has `use_ai_reach` switched off by a per-user overlay so it cannot send outreach mail, it cannot sign an agreement (that creates a real tenant) or change its email, and the owner audience gives it no route to the Super Admin console. (3) Email goes through the normal transport to any address. `DEMO_EMAIL_ALLOWLIST` and the withhold are removed. (4) The retention sweep `pickready.purge_public_demo`, `DEMO_RETENTION_DAYS` and the schedule rule are removed, so no application job deletes a demo user or tenant. Cleanup is a one-time, owner-confirmed operation. Real payment and real password authentication stay as A-23 left them. | Implemented in code, not yet deployed | `api/auth.py` (`demo_session`), `services/demo_access.py`, `services/security_email.py`, `workers/tasks.py` |
| A-03 | A security code (not a login OTP) serves company registration and password reset or change only, from the platform sender. The server resets and changes passwords and revokes every session. | Implemented | `services/security_codes.py` |
| A-04 | Google never authenticates a company role; candidates keep Google. Team members are invite-only and the server creates the password sign-in for exactly the invited email. | Implemented | `firebase_auth.assert_provider_allowed` |
| A-05 | Self-registration: register, security code, onboarding tenant, first plan payment, password, active. Payment is derived from billing rows, never stored; a browser callback never activates. A-23 amends the first-payment step only for public demo registration while `DEMO_MODE` is on. | Implemented | `services/company_onboarding.py` |
| A-06 | A session dies with the browser; the idle deadline (30 minutes) moves only on real user activity (`X-User-Activity`); Redis unavailable is 503, never fail-open. | Implemented | `services/auth_sessions.py` |
| A-07 | One person, one candidate record, resolved by `candidates.user_id`; email matching happens only at sign-in on a Firebase-verified address. | Implemented | `services/candidate_identity.py` |
| A-08 | Every tenant-scoped query goes through the RLS-aware session. A cross-tenant read answers 404. | Implemented | [../engineering/database.md](../engineering/database.md) |
| A-09 | Compensation never reaches a model. One module (`services/compensation_guard.py`) strips or redacts it; every prompt builder is registered in `test_ctc_never_in_prompt.py`. | Implemented | same test |
| A-10 | Candidate text and uploaded files are data, never instructions. Candidate projects are never executed. A hidden-text hit is provenance, not a rejection. | Implemented | [../engineering/security.md](../engineering/security.md) |
| A-11 | Embeddings are personal data at rest; erasure reaches vectors and caches. Delete My Profile deletes the Firebase identity in the same transaction. | Implemented | `pickready.cascade_erasure` |
| A-12 | Consent is per item with the verbatim sentence, version and digest, in an append-only table of its own. | Implemented | `consent_catalog.py` |
| A-13 | The application's database credential is its own (`pickready_app`), minted and rotated inside the platform; the DSN carries `ssl=require`. | Implemented | `scripts/rotate-app-db-credential.sh` |

## Job setup, contract and hiring

| ID | Decision | Status | Where |
|---|---|---|---|
| J-01 | Skills are drafted by Sutra from the JD (and the saved SWOT and Company Profile when they exist). The team edits and saves them: at most five per bucket (Must-have, Nice-to-have, Behavioural). No matrix, weight, priority or grade per skill is ever shown. | Implemented | [../spec/JOB_SETUP_FLOW.md](../spec/JOB_SETUP_FLOW.md) |
| J-02 | The **Final Job Posting** (skill names only, alphabetical, never evidence lines or priorities) is previewed before publish and is what candidates see. | Implemented | `assessment_contract.posting_skills` |
| J-03 | The first genuine application **freezes** the JD, title, experience band, grade and Skills, in the same transaction as the application. Refusals are one dated sentence (409). A sourced upload, databank link or invitation never freezes. | Implemented | `test_freeze_at_application.py` |
| J-04 | Candidates are assessed and graded against the locked contract snapshot (`assessment_contract`), never live skill rows. A snapshot is insert-only. | Implemented | `services/assessment_contract.py` |
| J-05 | Save Skills makes exactly one Sutra call (the recorded exception to "no slow work in a request"), before any write, and never holds the lock across it. | Implemented | `services/skills.py` |
| J-06 | A job lifecycle has six states: DRAFT, FINALIZED, PUBLISHED, CANDIDATE_APPLICATIONS, HIRING_PROCESS, CLOSED_ARCHIVED. J-17 will replace the approval part with Draft, Technical Review, HR Review, Approved and Published; the database mapping is unresolved ([OPEN.md](OPEN.md) T-01). | Implemented | `ck_jobs_lifecycle_state` |
| J-07 | A job is live 30 days plus 5 days grace; the dates are generated columns. Closing a job withholds its assessment data and purges it after 30 days; there is no reopen. | Implemented | [../spec/HIRING_WORKFLOW.md](../spec/HIRING_WORKFLOW.md) |
| J-08 | Sourced is not applied: a sourced candidate cannot be invited because the pipeline edge does not exist. The one invitation writer is `assessment_invitations.invite_batch`. | Implemented | [../spec/HIRING_WORKFLOW.md](../spec/HIRING_WORKFLOW.md) |
| J-09 | The BGV offer gate sits in `apply_transition`: a candidate with declared employers cannot be moved to offer until each is marked verified by a person. Rejection is never blocked. Nothing infers a verdict from a reply. | Implemented | `hiring_pipeline.py` |

## Assessment and grading

| ID | Decision | Status | Where |
|---|---|---|---|
| S-01 | **Miti is the sole grading authority.** Per skill: graded, unanswered (25, an unanswered Must-have fails) or not assessed (no score). No hash fallback. A model failure is "Not assessed", bounded to three attempts. | Implemented | [../spec/ASSESSMENT_FLOW.md](../spec/ASSESSMENT_FLOW.md) |
| S-02 | Questions: one per skill, never below the grade floor (10, 10, 10, 8), so 8 to 15; mix by largest remainder, prose 0.7, coding 0.2, objective 0.1. Coding only for a computing occupation (S-20). Every item is asked; there is no pre-fill, early close or extension. | Implemented | `services/assessment_questions/budget.py` |
| S-03 | The **server keeps the time** (turn stamps, allocation snapshots, pause rows); the client reports no durations. Every answer names `turn_seq`; a stale one is 409. | Implemented | `services/assessment_conversation/timers.py` |
| S-04 | Yukti reads resumes against the saved Skills on fixed parts and blends into one rank key derived in SQL, with the Must-have cap outermost. A model failure is "not assessed", never a substitute score. | Implemented | `services/yukti/` |
| S-05 | Siddhi withholds any statement without a supporting citation and routes the report to a person. There is no bypass flag. | Implemented | `services/siddhi/` |
| S-06 | Evidence RAG is reached only through the tool layer; every registered tool is a bounded read; Miti and Siddhi import neither retrieval nor the index. | Implemented | `test_evidence_rag_wiring.py` |
| S-07 | The PRISM PDF leaves only through gate G4 and the candidate's retention consent. | Implemented | `siddhi/delivery.py` |

## Data, retention and legacy

| ID | Decision | Status | Where |
|---|---|---|---|
| R-01 | Closing a job is a 30-day soft deletion; objects are deleted before rows; a purge that cannot confirm every object gone deletes nothing and retries. | Implemented | `services/job_assessment_retention.py` |
| R-02 | No irreversible drop of a table that may hold customer rows; a table proven empty may be dropped by a migration that counts first and raises. | Implemented | [../operations/LEGACY_TABLES.md](../operations/LEGACY_TABLES.md) |
| R-03 | A project upload is staged temporarily and deleted after derived evidence is persisted; the product stores intelligence, not the project. | Implemented | [../spec/PROJECT_EVIDENCE_INTELLIGENCE.md](../spec/PROJECT_EVIDENCE_INTELLIGENCE.md) |

## Platform and infrastructure

| ID | Decision | Status | Where |
|---|---|---|---|
| I-01 | Hosting is AWS (ECS Fargate, Lambda, RDS PostgreSQL with pgvector, ElastiCache Redis, S3). Pilot (`readypick-pilot`, `ap-south-2`) is the live site; staging and production are defined, not applied. | Implemented | [../operations/INFRA_TOPOLOGY.md](../operations/INFRA_TOPOLOGY.md) |
| I-02 | **No Celery.** Slow work is dispatched (`dispatch`, `dispatch_after_commit`) to Lambda or on-demand Fargate; every `@task` declares `rls`. | Implemented | [../spec/BACKGROUND_WORK.md](../spec/BACKGROUND_WORK.md) |
| I-03 | One model vendor, closed mapping: `gpt-5.6-terra` (judge and write), `gpt-5.6-luna` (extract and classify), `voyage-4` (embeddings), plus the rerank model. No fallback chain. | Implemented | `config/llm_providers.py` |
| I-04 | `main` is the only deployable branch; pilot is built and deployed from a commit on `main`, verified by image digest. | Implemented | [../operations/BRANCH_PROTECTION.md](../operations/BRANCH_PROTECTION.md) |
| I-05 | Development storage hygiene: one Compose project, temporary worktrees, reproducible artifacts are not data. | Implemented | [../development-storage-policy.md](../development-storage-policy.md) |
| I-06 | OpenTelemetry is the only tracer. | Implemented | `services/observability/otel.py` |

---

## Pending changes

Code, configuration or deployment work the owner has decided and nobody has done. A documentation-only task never does these. Delete a row only when the change has shipped and its owning document is updated in the same change.

| # | Change | Driven by | Touches |
|---|---|---|---|
| 1 | Add the states Draft, Technical Review, HR Review, Approved, Published with correction and resubmission; HR approval, independence rule, revision pinning and the Recruiter-only publish path; remove HR Manager publish; Hiring Manager assignment by Recruiter, HR Manager and Super Admin; SWOT complete before invitations are sent, frozen at first assessment start; preserve earlier assessment snapshots when a SWOT changes after publication | J-10 to J-21 | `services/rbac`, `services/capabilities.py` (+ seeding migration), `api/job_setup.py`, `api/jobs.py`, lifecycle CHECK, invitation gate, frontend job page, tests |
| 2 | Remove the sandbox: `services/code_execution/`, `coding_assessment` run and key modules, `coding_question_keys`, Judge0 Terraform and settings, hidden-test scoring; add the static checker and AI review path | S-20 | backend, `infra/modules/code_sandbox`, `docs` |
| 3 | Switch email to SES only: remove the SMTP path and `email_transport`; send security codes from `noreply@readypick.ai` and other platform and fallback recruitment mail from `contact@readypick.ai`; replace `noreply@pickready.app` (config default and test fixture) and `hello@pickready.app` (two billing-page links) | I-20 | `core/config.py`, `services/email*`, `infra/`, `.env.example`, `frontend/app/(org)/org/billing/page.tsx` |
| 4 | Call the retrieval-time injection screen on every retrieval path; quarantine, exclude from context and record each hit | A-20 | `services/rag/`, `services/evidence_retrieval.py`, tests |
| 5 | Remove the Graphify `PreToolUse` hooks from the untracked local `.codex/hooks.json` (`.claude/settings.json` is done) | W-20 | `.codex/hooks.json` |
| 6 | Yukti five-part model 45/15/20/15/5 with compensation, notice period and document readiness shown separately | S-21 | `services/yukti/config.py`, prompt, projection, tests |
| 7 | Apply the production guards whenever real users are served, whatever `ENVIRONMENT` is called (today `ENVIRONMENT=pilot` leaves five guards inert) | A-22 | `core/config.py` (`is_production`), `infra/`, tests |
| 8 | Replace the monthly plans with per-credit pricing once the owner settles price, GST, expiry, refunds and the transition for existing subscribers | C-10 | `services/monthly_plans.py`, `credits.py`, `credit_packs.py`, billing pages, migration, tests |
