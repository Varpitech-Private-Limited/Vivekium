# Open decisions and unresolved contradictions

**Status:** current. **Reviewed:** 2026-10-14 (after the second owner ruling).
**Rule:** nothing on this page is approved. A default named here is what the code does today, not a ruling. When the owner answers, move the answer to [REGISTER.md](REGISTER.md) and delete the row here in the same change.
**Question format for the owner:** state the current behaviour, the problem, a real example, what each option causes, and a recommendation ([../engineering/documentation-policy.md](../engineering/documentation-policy.md)).

## A. Owner question still unanswered

| ID | Open point | Why it matters | Default until ruled |
|---|---|---|---|
| Q-30 | **Which legal obligations apply to the candidate-facing status** (A-21 is "subject to applicable legal obligations"). The 2026-09-08 brief named Article 15(1)(h) as a driver; no jurisdiction has been ruled on. | A status-only policy may be too thin in some jurisdictions. | Recruiter-facing explanations only; none built for candidates. |
| Q-34 | **What to do with candidate verification documents already uploaded** (academic certificate, address proof), now that upload is retired. Options: keep until the candidate deletes their profile, purge all after notice, or purge by age. Plan: [../operations/BGV_LEGACY_DOCUMENTS.md](../operations/BGV_LEGACY_DOCUMENTS.md). | Holding identity-adjacent files with no purpose or sharing consent is a privacy exposure; deleting without authority breaks retention. | Keep; nothing is deleted. A candidate can still withdraw a file or delete their profile. |

## B. Unresolved technical work (recorded, not decided)

The 2026-10-14 rulings leave these open. None has an owner answer; none is assumed.

| ID | Open point |
|---|---|
| T-01 | **Job state machine.** The exact database states, allowed transitions, correction and resubmission paths and the migration design for J-17. How the five names map onto the six states in `ck_jobs_lifecycle_state` (J-06). |
| T-02 | **Coding checks (S-20).** Languages, static-checker tools per language, the scoring proportion between checker and AI review, and what feedback the candidate sees. |
| T-03 | **Email (I-20).** SES identity verification for `noreply@` and `contact@`; what development and tests use once SMTP is removed; what `manjunath@readypick.ai` receives (inbound routing, support notices). |
| T-04 | **Credit pricing (C-10).** Conversion rates for existing subscribers, treatment of unused plan benefits and rollover credits, customer notification terms, credit price, GST treatment, consumption, expiry, refunds and billing transitions. Settled: subscribers keep their paid period and move to credits at their next renewal. |
| T-05 | **Scoring policy versioning (S-21).** What a policy version stores, and how a historical score is reproduced after the weights change. |
| T-06 | **Assessment snapshot consistency (J-18, J-19).** How HR review of a post-publication SWOT change gates future assessments, how snapshots stay consistent with a live SWOT, and how concurrent edits to a job are handled. |
| T-07 | **Remaining security, infrastructure, CI/CD and technical-debt findings:** Q-21 to Q-27, Q-29, Q-31, Q-32 below. |
| T-08 | **Material-change field list (J-13).** Which exact fields of the JD, hiring requirements, Skills, compensation and SWOT count as material; the rule is settled, the list is not written. |
$(printf '%s' |

## C. Carried over from earlier records, never ruled

| ID | Open point | Default today |
|---|---|---|
| Q-20 | A designed logo mark (today a drawn V). | The drawn V. |
| Q-21 | `LLM_KEY_ENCRYPTION_SECRET` is held and granted to no service. | Held. |
| Q-23 | The `impeccable` design tool is unpinned by hash and gates CI. | Unpinned. |
| Q-24 | Inbound reply email is inert on pilot (`INBOUND_EMAIL_DOMAIN` empty); `transcribe_enabled` off until the deploy stage. | Inert, off. |
| Q-25 | Five one-tenant tasks still bypass RLS pending a real-Postgres proof (`generate_job_swot`, `draft_job_skills`, `execute_coding_submission`, `transcribe_voice_answer`, `process_assessment_video`). `execute_coding_submission` goes with the sandbox (Pending change 2). | Bypass, with a written reason each. |
| Q-26 | Leftovers: `agent_execution_traces` has no writer; `RequestTrace.add_cost` has no caller; the older per-key JD path (`jd_generation`, `jd_generation_system.txt`) survives; column `hm_access_granted` has no model attribute; the request coalescer has four uncalled functions. | Kept. |
| Q-27 | Documentation figures never re-measured: test counts and baselines recorded on 2026-08-29 ([../history/AUDITS.md](../history/AUDITS.md)); `PRODUCT.md`, `docs/spec/RBAC_SPECIFICATION.md` and the Runbook were scanned for retired product terms only. The `DESIGN.md` logo and palette contradiction was resolved against the owner's 2026-10-17 reference. | Treat the remaining figures as stale. |
| Q-29 | Never verified (2026-09-17 security pass): the CSP against the Razorpay checkout and the Google sign-in popup; whether any secret still holds `PLACEHOLDER_NOT_CONFIGURED`; rate limits under load. | Unverified. |
| Q-31 | Durable execution for long workflows (brief item W5.2: Lambda durable functions or DBOS). | No ruling; not built. |
| Q-32 | Technical debt from the 2026-09 audits: pilot is single-AZ (`multi_az = false`; must flip before real tenant data); `audit_log` has no index serving the cross-tenant ordered scan; the two JSON-LD payloads are client-rendered; no distributed tracing across dispatch hops; retrieval quality unmeasured (golden set 60 of 300, none human verified); shadow eval, tenant canary and drift watch are designed, not built. | Accepted debt. |
| Q-33 | Two frontend advisories remain after the 2026-10-14 dependency fix: `braces` 3.0.3 (high, no patched version published) and `postcss-selector-parser` 6.1.4 (medium; fixed by a major bump that arrives with a Tailwind 3 upgrade). Neither fails the Security scan. | Open, accepted. |

## D. Contradictions found by the audits

"Resolved" means the latest owner statement or the code was taken as the winner and the document corrected. "Open" means a person must choose.

| ID | Contradiction | Result |
|---|---|---|
| X-01 | The owner said the coding sandbox "should not stay on hold, it is mandatory", then that it is "not necessary". | Resolved by the later statement and confirmed in the second ruling (S-20). |
| X-02 | Approval wanted back (J-10) while a Hiring Manager's SWOT edits are allowed after publish (J-16). | Resolved in part by J-18: a SWOT edit does not unpublish. The remainder is X-12. |
| X-03 | RBAC spec (rank 1) has no HR approval step and gives HR Manager "Publish YES*". | Resolved: J-10 to J-12 amend it. The filed text is left unedited; an amendment note points to the register. |
| X-04 | The 2026-08-23 rule gave the PRISM subtitle as "Predictive Role Intelligence & Suitability Mapping"; the 2026-09-20 rename and the code say "Evidence-Based ...". | Resolved: the code and the later rule win (P-04). |
| X-05 | Documents said the Runbook is v1.1; the file is v1.4. | Resolved: v1.4. |
| X-06 | The ESD named the approval state machine and the matching pipeline as "unchanged and authoritative". Both were replaced in 2026-09-25. | Resolved: ESD rewritten. |
| X-07 | Deployment runbooks said "nothing has been executed" while pilot has been live since 2026-09. | Resolved: status banners corrected. |
| X-08 | Instruction files claimed migrations 0001 to 0130; the head is 0136. | Resolved. |
| X-09 | The 2026-09-06 rule allowed SMTP or SES; I-20 allows SES only. | Resolved by I-20; code pending. |
| X-10 | The "MANDATORY graphify query" instruction against W-20. | Resolved by W-20; Claude hook removed, Codex hook file pending. |
| X-11 | C-10 (per-credit pricing) against the 2026-09-30 monthly plans and the live code. | Resolved in intent: per-credit replaces subscriptions; existing subscribers keep their paid period and move at next renewal. The code still runs the plans (Pending change 8); transition terms are T-04. |
| X-12 | J-13 (a material SWOT change cancels approval) against J-18 (a post-publication SWOT change does not unpublish). | Resolved: pre-publication approval is invalidated by a material change; after publication the job stays live and HR review is needed before the change affects future assessments (J-18). The gating mechanics are T-06. |
| X-13 | **J-14 (a Super Admin may self-approve when no independent approver exists) against the later answer that the Super Admin follows the same steps.** The owner kept both: the Super Admin cannot skip a step but may be the approver in the audited small-company case. | Resolved as written in J-11 and J-14; recorded here so nobody reads them as a conflict. |
