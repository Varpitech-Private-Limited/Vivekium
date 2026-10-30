# Rulings log, 2026-07 to 2026-10

**Status:** history record. Not an instruction. Current rules are in [../decisions/REGISTER.md](../decisions/REGISTER.md) and [../engineering/](../engineering/).
**What this is:** a one-line-per-ruling summary of the old `CLAUDE.md` (367 KB, reverse-chronological, the last version is commit `73103c3`). Retrieve the full original text with `git show 73103c3:CLAUDE.md`. Everything still in force was carried into the register and the engineering documents; this log keeps the reversals and the reasons so nobody re-introduces them.

## Reversals and replacements (newest first)

| Date | What changed | Replaced | Where it lives now |
|---|---|---|---|
| 2026-10-30 | Server-drawn text CAPTCHA removed from every auth surface. No challenge is drawn and no proof is checked. Nothing pretends a proof was checked. `captcha_purpose` stays and is only the portal filter. `captcha_proof` from an old client is not read. Rate limits, the six-digit security code, Firebase identity, authorization and tenant isolation stay | A-02 (2026-09-29), CAPTCHA everywhere | Register A-24 |
| 2026-10-14 (second ruling) | Job states Draft, Technical Review, HR Review, Approved, Published; SWOT edits after publishing never unpublish; SWOT complete before invitations and frozen at first assessment start; assignment by Recruiter, HR Manager, Super Admin; coding by static checker plus AI review with no hidden-test scoring; Yukti five-part score 45/15/20/15/5 approved; SES only with `noreply@` for security mail and `contact@` for the rest; injection screen quarantines; per-credit pricing only; recruiter-facing explanations only; production controls whenever real users are served | The unnamed approval states of J-10, the "provisional" Yukti weights, the first I-20 text, the 2026-09-30 monthly plans (in intent), the open questions Q-01, Q-02, Q-03, Q-04 to Q-12, Q-28, X-11, X-12 | Register J-17 to J-21, S-20, S-21, A-20 to A-22, I-20, C-10 |
| 2026-10-14 | Approval flow Recruiter, Hiring Manager, HR, Recruiter publishes; SWOT editable after publishing; coding without a sandbox; email SES only; injection screen mandatory; Yukti weights provisional | v10 publish rule, the 2026-09-25 approval-chain deletion, the sandbox design, the 2026-09-06 email-transport rule | Register J-10 to J-16, S-20, S-21, I-20, A-20 |
| 2026-10-07 | Development storage and workspace hygiene made permanent after 75.6 GB of stale worktrees and 129 GB of Docker data | none | [../development-storage-policy.md](../development-storage-policy.md), [../engineering/infrastructure.md](../engineering/infrastructure.md) |
| 2026-09-30 | Four monthly plans; paid Starter pilot; three-month rollover; Starter-only top-up; no setup fee | The 2026-09-29 "credits are the only model, subscriptions deleted" | Register C-01 (live code); intent superseded by C-10; terms in [SUPERSEDED.md](SUPERSEDED.md) F |
| 2026-09-29 | CAPTCHA everywhere; security codes; server-side password reset; self-registration activated by first payment; CEO, MD, Functional Head roles; Leadership Intelligence | Drishti as the leadership input; Firebase's own reset email; login by Google for company roles | Register A-03 to A-05, P-11, P-12. CAPTCHA everywhere (A-02) was reversed on 2026-10-30 by A-24; see the 2026-10-30 row |
| 2026-09-28 | CONTRACT v10: Skills drafted from the JD, SWOT separate, the Final Job Posting, freeze at the first genuine application | D1's JD-SWOT-Skills order; D5's lock at the first start | Register J-01 to J-03; [../spec/JOB_SETUP_FLOW.md](../spec/JOB_SETUP_FLOW.md) |
| 2026-09-28 | Coding sandbox put on hold (resources destroyed) | The 2026-09-25 plan to ship Judge0 | Superseded by 2026-10-14 S-20 |
| 2026-09-25 | The Vivekium simplification release: Skills step and locked contract replace the matrix and Save Matrix; Yukti's six parts and one blended rank; no number with no exception (`match_percent` deleted); server-timed assessment; Path P device pauses; Miti the sole grader; insert-only report; Evidence RAG through tools; dispatch after commit and per-task RLS; legacy scrap (migrations 0118 to 0131) | Matrix, Save Matrix, Tatva human-authority freeze, approval chain, video interview mode, resume pre-fill, retake, hash scorer, LangSmith, phone sign-in, five-band vocabulary, prescreen letter grades | Register; [../spec/](../spec/) |
| 2026-09-23 | Tatva human authority: Sutra proposes, the Hiring Manager owns the criteria, Save Matrix freezes | Compiler-owned matrix | Replaced 2026-09-25; only the "model work and validation before the first write" ordering survives |
| 2026-09-22 | Media storage required (P1 reversed); job closure becomes a 30-day soft deletion; credit expiry for new grants only; the harness | "No media is ever stored"; immediate hard delete at closure | Register P-08, R-01; [../spec/HARNESS.md](../spec/HARNESS.md) |
| 2026-09-21 | Soft-deleted names are revived, never re-inserted; an emptied set is not an ungenerated one; matrix editor became chips | A 500 on re-adding a removed name | [../engineering/database.md](../engineering/database.md) |
| 2026-09-20 | Product renamed Vivekium; browser-session auth with idle deadline; Role Intake deleted; audit rows written in one INSERT | ReadyPick as product name | Register P-01, A-06 |
| 2026-09-18 | Owner brief declared the final source of truth; `match_percent` the one sanctioned number; seven derived recruiter columns | The no-exception number rule (restored 2026-09-25) | Register P-02 |
| 2026-09-16 | Realtime hub shutdown and per-loop binding; `py-spy` for hangs | A process-wide singleton that outlived its loop | [../engineering/backend.md](../engineering/backend.md), [../engineering/testing.md](../engineering/testing.md) |
| 2026-09-13 | One read-only sentence, capability-first UI; occupational STEM classification; AI-drafted Job SWOT | Role-name branching in the UI; keyword STEM | [../engineering/frontend.md](../engineering/frontend.md) |
| 2026-09-12 | Employment declaration and the offer gate; native chat; reply address and inbound SES parser (inert on pilot) | A verification system keyed by address | Register J-09; [../spec/CANDIDATE_COMMUNICATIONS.md](../spec/CANDIDATE_COMMUNICATIONS.md) |
| 2026-09-11 | The application's own database credential, TLS on the DSN, Provider may set a customer's primary contact | A copy of the RDS master password that AWS rotated (a full outage) | Register A-13 |
| 2026-09-10 | Native support surface replaces the Intercom integration; no EEO reporting or impact ratios; RDS proxy refused; reports carry model and prompt provenance | Intercom; a proxy that pins every session | Register P-14 |
| 2026-09-09 | AI runtime upgrade (retrieval index, tool firewall, eval, sufficiency gate, AI activity); Company DNA removed and the Company Profile replaces it | Company DNA questionnaire; a 19,000-line wiring workstream that grep falsely demanded | [../architecture/AI_RUNTIME.md](../architecture/AI_RUNTIME.md), [../engineering/ai-runtime.md](../engineering/ai-runtime.md) |
| 2026-09-06 | Corporate senders; dual-mode assessment; video access; retention consents; employer pages; intelligence dashboards | "OTP nowhere", "Gmail SMTP only" (narrowed) | Mostly superseded 2026-09-25; corporate senders stand |
| 2026-09-05 | Celery deleted; work dispatched to Lambda and on-demand Fargate | Celery, beat, a result backend | [../spec/BACKGROUND_WORK.md](../spec/BACKGROUND_WORK.md) |
| 2026-09-04 | The eight hiring gates; the sourced stage; the final ranking; the Updates feed; job closure | Pre-assessment order as the permanent order | [../spec/HIRING_WORKFLOW.md](../spec/HIRING_WORKFLOW.md) |
| 2026-09-02 | Mandatory proctoring; six question formats; evidence dominance | An optional screen-capture consent | [../spec/PROCTORING.md](../spec/PROCTORING.md) |
| 2026-09-01 | Project evidence intelligence: derived evidence stored, originals temporary; the documentation folder created | Storing candidate projects | [../spec/PROJECT_EVIDENCE_INTELLIGENCE.md](../spec/PROJECT_EVIDENCE_INTELLIGENCE.md) |
| 2026-08-31 | Vendor changed from Anthropic to OpenAI models; `voyage-context-4` found not to exist, `voyage-4` used | Anthropic as the model vendor | Register I-03 |
| 2026-08-29 | spec-doc6: RBAC and Runbook reconciled; the Runbook's data became YAML with a parity test; Part A activated | Nine wrong assumptions about the Runbook | [../reference/RBAC.md](../reference/RBAC.md) |
| 2026-08-28 | spec-doc5: three-layer hiring framework, single vendor, navy and teal UI, AWS-ready | Groq, Gemini, OpenRouter, GCP | Largely superseded; design tokens remain |
| 2026-08-23 | Tatva Assessment is the process, PRISM Report the document; three radar charts; fixed section order | PPI as the visible name; four charts | Register P-04 to P-06 (the subtitle later changed to "Evidence-Based ...") |
| 2026-08-18 | Ten-system agent framework: tools raise, loops degrade, retrieval is chunk-level, budgets refuse before work | Ad hoc agent code | [../engineering/ai-runtime.md](../engineering/ai-runtime.md) |
| 2026-08-14 | Spec v4: hierarchical staff roles; credit gates immediate; customer AI dashboard deleted | A flat staff model | Register P-11 |
| 2026-08-06 | No preset technical question bank; every generative task inside a bounded loop | A per-job stored question list | [../engineering/backend.md](../engineering/backend.md) |
| 2026-08-05 | Adaptive interview, non-answer handling, guardrails, telemetry; demo tenants | A scripted index | Largely replaced by the 2026-09-25 assessment |
| 2026-07-30 | One four-grade scale; PPI replaces PFI; the review gate on the framework; six mandatory validation fields | Two five-label scales; one fixed index per grade | Register P-03 |
| 2026-07-28 | Credit ledger in integer sub-units; fourth portal (Business Development); one markdown JD | Floats; per-section JD boxes | Register C-06 |
| 2026-07-27 | Provider portal read-only by absence; 30 plus 5 day posting window; ten-stage pipeline; no numbers to a client | none | Register J-07, P-02 |
| 2026-07-26 | Grade drives assessment counts; scoring reads real answers | A hash that decided grades | Register S-01 |

## Crosswalk: old `CLAUDE.md` section to today

Code comments cite sections by date ("the 2026-09-16 section of CLAUDE.md"). Use this table to resolve one.

| Old section (date) | Today |
|---|---|
| Rules that break the most builds; sections 1 to 8 (conventions, layout, env, local dev, where to change) | [../../CLAUDE.md](../../CLAUDE.md), [../engineering/where-to-change.md](../engineering/where-to-change.md), [../../CONTRIBUTING.md](../../CONTRIBUTING.md) |
| How to work in this repository | [../../CLAUDE.md](../../CLAUDE.md), [../engineering/agent-workflow.md](../engineering/agent-workflow.md) |
| Development storage and workspace hygiene (2026-10-07) | [../development-storage-policy.md](../development-storage-policy.md) |
| Monthly plans (2026-09-30); onboarding, auth, credits, Leadership (2026-09-29) | Register C-*, A-*, P-11, P-12. A-02 (CAPTCHA everywhere) was reversed 2026-10-30; see the 2026-10-30 row and register A-24 |
| Vivekium simplification release and CONTRACT v10 (2026-09-25 and 2026-09-28) | [../spec/JOB_SETUP_FLOW.md](../spec/JOB_SETUP_FLOW.md), [../spec/ASSESSMENT_FLOW.md](../spec/ASSESSMENT_FLOW.md), [../spec/PROCTORING.md](../spec/PROCTORING.md), [../spec/RETRIEVAL.md](../spec/RETRIEVAL.md), [../spec/CANDIDATE_COMMUNICATIONS.md](../spec/CANDIDATE_COMMUNICATIONS.md), [../engineering/](../engineering/) |
| Candidate code in one sandbox (2026-09-25) | Superseded; [../spec/CODING_ASSESSMENT.md](../spec/CODING_ASSESSMENT.md) (decided) and [SUPERSEDED.md](SUPERSEDED.md) |
| Tatva human authority (2026-09-23) | [SUPERSEDED.md](SUPERSEDED.md) |
| Change requests and the harness (2026-09-22) | [../spec/HARNESS.md](../spec/HARNESS.md), [../engineering/testing.md](../engineering/testing.md) |
| Thirty-day soft deletion (2026-09-22) | Register R-01 |
| Soft delete and the hard constraint (2026-09-21) | [../engineering/database.md](../engineering/database.md) |
| Vivekium release (2026-09-20); vivekium ruling (2026-09-18) | Register P-01, A-06 |
| Singleton that outlived its loop (2026-09-16) | [../engineering/backend.md](../engineering/backend.md), [../engineering/testing.md](../engineering/testing.md) |
| Permission-aware UX, STEM, Job SWOT (2026-09-13) | [../engineering/frontend.md](../engineering/frontend.md), [../spec/JOB_SETUP_FLOW.md](../spec/JOB_SETUP_FLOW.md) |
| BGV and conversations (2026-09-12); rotated credential (2026-09-11); native support (2026-09-10) | Register J-09, A-13; [../spec/CANDIDATE_COMMUNICATIONS.md](../spec/CANDIDATE_COMMUNICATIONS.md); [../engineering/security.md](../engineering/security.md) |
| AI runtime upgrade (2026-09-09); Company DNA removed (2026-09-09) | [../engineering/ai-runtime.md](../engineering/ai-runtime.md), [../architecture/AI_RUNTIME.md](../architecture/AI_RUNTIME.md) |
| Add-features release (2026-09-06) | [SUPERSEDED.md](SUPERSEDED.md); corporate senders in [../spec/CANDIDATE_COMMUNICATIONS.md](../spec/CANDIDATE_COMMUNICATIONS.md) |
| Background work without Celery (2026-09-05) | [../spec/BACKGROUND_WORK.md](../spec/BACKGROUND_WORK.md) |
| End-to-end hiring workflow (2026-09-04) | [../spec/HIRING_WORKFLOW.md](../spec/HIRING_WORKFLOW.md) |
| Proctoring and question formats (2026-09-02) | [../spec/PROCTORING.md](../spec/PROCTORING.md), [../spec/ASSESSMENT_QUESTION_FORMATS.md](../spec/ASSESSMENT_QUESTION_FORMATS.md) |
| Project Evidence Intelligence (2026-09-01); documentation layout (2026-09-01) | [../spec/PROJECT_EVIDENCE_INTELLIGENCE.md](../spec/PROJECT_EVIDENCE_INTELLIGENCE.md); [../engineering/documentation-policy.md](../engineering/documentation-policy.md) |
| spec-doc6 (2026-08-29): RBAC, Runbook data, precedence | [../reference/RBAC.md](../reference/RBAC.md), [../engineering/documentation-policy.md](../engineering/documentation-policy.md), [../product/Readypick%20Hiring%20Philosophy.md](../product/Readypick%20Hiring%20Philosophy.md) |
| spec-doc5 (2026-08-28): framework, vendor, UI, AWS | [../engineering/ai-runtime.md](../engineering/ai-runtime.md), [../engineering/frontend.md](../engineering/frontend.md), [../engineering/infrastructure.md](../engineering/infrastructure.md) |
| Tatva and PRISM (2026-08-23) | Register P-04 to P-06 |
| Ten-system agent framework (2026-08-18) | [../engineering/ai-runtime.md](../engineering/ai-runtime.md) |
| Product spec v4 (2026-08-14) and everything older | Register; [SUPERSEDED.md](SUPERSEDED.md) |
| The `claude.md` / `ai-upgrade-spec-doc.md` / `specdoc4` names in code comments | `claude.md` is `CLAUDE.md`; `ai-upgrade-spec-doc.md` is now [SUPERSEDED.md](SUPERSEDED.md) (original: `git show 73103c3:docs/spec/AI_RUNTIME_UPGRADE.md`) |
