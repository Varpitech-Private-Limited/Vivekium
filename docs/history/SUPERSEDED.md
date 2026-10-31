# Superseded designs and specifications

**Status:** history record. Not an instruction. Nothing here describes how the product works today.
Each entry: what it was, why it is obsolete, what replaced it, and the retrieval command. The last commit before the 2026-10-14 documentation restructure is `73103c3`; `git show 73103c3:<path>` prints the original. (That commit sat on the local branch `codex/monthly-pricing`. If a squash merge ever makes it unreachable, find the parent of the restructure commit instead, or tag it first: `git tag pre-docs-restructure 73103c3`.)

## A. Sandbox-executed coding assessment (2026-09-25, on hold 2026-09-28, superseded 2026-10-14)

- **What:** candidate code ran in one sandbox (Judge0) behind a provider port; hidden tests sat in a private answer-key table with one reader; a coding question was accepted only after the sandbox proved the reference solution; Run was interactive, Submit stored the answer and executed after commit; score was 70 percent hidden tests and 30 percent AI quality review; sweeps reconciled submissions; the editor was Monaco.
- **Why obsolete:** the owner ruled on 2026-10-14 that no sandbox is required (register S-20). The sandbox was never provisioned in earnest: stage A resources were destroyed on 2026-09-28 and `CODE_EXECUTION_BACKEND` shipped `disabled`.
- **Replaced by:** [../spec/CODING_ASSESSMENT.md](../spec/CODING_ASSESSMENT.md).
- **Originals:** `docs/spec/CODE_EXECUTION.md`, `docs/operations/JUDGE0_RUNBOOK.md`.
- **Kept ideas:** the answer key judges and never discloses; a provider never receives an expected output; a failed tool is a state, never a substitute grade.

## B. AI runtime upgrade brief, RPN-AI-UP-001 (2026-09-08)

- **What:** a 163 KB owner brief (precedence rank 3a) with eleven workstreams: W0 baseline, W1 wiring, W2 retrieval, W3 safety and memory, W4 latency, W5 durability, W6 sufficiency and ledger, W7 eval, W8 release gate, W9 and W10 operations, W11 compliance explanation.
- **Why obsolete:** it named Company DNA as live and Gemini as a judge vendor (both removed), its section 2 audit was wrong (the framework was already wired), and most workstreams shipped in 2026-09-09 to 2026-09-10.
- **Replaced by:** [../architecture/AI_RUNTIME.md](../architecture/AI_RUNTIME.md) (as built), [../spec/RETRIEVAL.md](../spec/RETRIEVAL.md), [../engineering/ai-runtime.md](../engineering/ai-runtime.md), the baseline [../verification/AI_UPGRADE_BASELINE.md](../verification/AI_UPGRADE_BASELINE.md).
- **Original:** `docs/spec/AI_RUNTIME_UPGRADE.md`.
- **Rejected on evidence, kept so nobody re-argues them:** query rewriting, HyDE and multi-query (prompt-only refinement lowered nDCG@10 by 9 percent on a stable jargon corpus); semantic chunking (no gain for the cost); GraphRAG (the unit is one candidate against one job, a local query); a second agent framework; Temporal, Restate or Inngest (standing infrastructure the Celery removal deleted); request hedging (doubles cost); RAGAS as a gate (its judge is not reproducible here); promptfoo as the primary harness (owned by the vendor whose model is gated); a non-commercial reranker licence; Postgres extensions unavailable on RDS; fine-tuned judges (poor off-scheme and on fairness); "twenty more agents" (do not parallelise parts that must agree).
- **Its open owner decisions** were resolved elsewhere or remain in [../decisions/OPEN.md](../decisions/OPEN.md): per-tenant learnings (resolved, 2026-09-09), EEO collection (refused, 2026-09-10), durable execution and the candidate-facing explanation of automated logic (no ruling on record; Q-30, Q-31).

## C. Sprint feature brief (2026-09, "vivekium.pdf") and its reconciliation

- **What:** eight features: Drishti as a seventh agent, a resume-aware questionnaire, a seven-column recruiter home page, BGV automation, two data lifecycles, a two-stage six-item consent framework, Delete My Profile, consent renewal; plus a conflict register C1 to C7 of places the brief and a standing rule could not both hold.
- **Why obsolete:** every feature was built or deliberately replaced. Resolutions that mattered: C1 a match percentage (allowed 2026-09-18, deleted 2026-09-25); C2 a 40-question ceiling and resume pre-fill (deleted 2026-09-25); C3 Drishti against the Company DNA removal (Drishti became optional context text, then Leadership Intelligence, 2026-09-29); C5 and C6 closure deletion and the two calendars (now the 30-day soft deletion plus erasure); C7 forbidden terms and the em dash (kept).
- **Replaced by:** [../decisions/REGISTER.md](../decisions/REGISTER.md) and the specs.
- **Original:** `docs/spec/VIVEKIUM_SPRINT_FEATURES.md`.

## D. Architecture direction, design review of 2026-08-28 (advisory)

- **What:** progressive investigation instead of an elimination funnel, a signal taxonomy under five dimensions, evidence sufficiency distinct from confidence, an investigation engine, constitutional escalation, adaptive assessment, AI-assistance as an integrity signal never a penalty, and what was decided not to be built.
- **Why obsolete:** it was advisory from the start and described a Part A matrix pipeline that the 2026-09-25 release replaced with Skills, Yukti and Miti. Principles that survive are in [../engineering/ai-runtime.md](../engineering/ai-runtime.md) (insufficient is not negative; no flag auto-rejects; two benign explanations before escalation).
- **Original:** `docs/spec/ARCHITECTURE_DIRECTION_2026-08-28.md`.

## E. Tatva matrix, Save Matrix, human-authority freeze (to 2026-09-25)

- **What:** Sutra compiled a seven-stage matrix (competency, observable evidence, sources, method, weight, threshold, disqualifier) under three layers (baseline, situation, role); the Hiring Manager reviewed it; Save Matrix enriched and froze exactly the reviewed matrix; reopen was bounded by an issued contract; 15 department models and situation types fed the weights.
- **Why obsolete:** the 2026-09-25 release replaced the matrix with the Skills step and a locked snapshot; no weight is derived or stored on the live path; Company DNA was removed 2026-09-09. The seven-stage columns remain in the schema as history.
- **Replaced by:** [../spec/JOB_SETUP_FLOW.md](../spec/JOB_SETUP_FLOW.md).

## F. Retired pricing models

- **Subscriptions with plan tiers and pre-2026-09-29 plan columns** (to 2026-09-29), then **credits only with subscriptions deleted** (2026-09-29): replaced on 2026-09-30 by four monthly completed-assessment plans (register C-01 to C-07). Historical purchases and invoices stay readable at the terms they were sold on.
- **Four monthly completed-assessment plans (2026-09-30)**: Starter 75 for INR 24,000, Growth 200 for INR 55,000, Scale 500 for INR 1,20,000, Pro 1,200 for INR 2,40,000 per month, before GST; monthly renewal, no annual commitment, cancel at the end of the paid cycle, no setup fee; a paid 30-day Starter pilot; one credit per completed assessment (including STEM), none for incomplete assessments, no-shows or old-profile reviews; credits pool across open jobs and roll over three months; only a Starter pack (75 for INR 24,000) is sold as a top-up; public pricing sits in Start beside "Open a role" and never shows derived per-assessment prices. **Superseded in intent on 2026-10-14 by per-credit pricing only (register C-10); the code still implements it; existing subscribers keep their paid period and move to credits at their next renewal, other terms open (OPEN T-04).** Recover the originals with `git show 73103c3:docs/product/PRD.md`.
- Credits granted before expiry existed never expire; new grants roll over three months.

## G. Operational notes that no longer apply

- `docs/operations/DATABASE_CREDENTIAL_MIGRATION.md`: the 2026-08-24 fix for a `DATABASE_URL` composed at deploy time on Cloud Run. The platform is gone; the finding lives on as the per-service IAM scoping rule ([../engineering/infrastructure.md](../engineering/infrastructure.md)).
- `docs/operations/PICKREADY_APP_ADDRESSES.md`: the two `pickready.app` mailboxes (`noreply@` as a config default, `hello@` on two billing-page links, and a test fixture). The owner ruled they are obsolete (register I-20); the replacement is Pending change 3.
- `docs/operations/TEST_BASELINE.md`: test-suite counts recorded on 2026-08-29 against branch `feat/specdoc6-activation` (then 1755 passed, 10 failed, 51 skipped, all failures environmental). The numbers are long stale; current counts come from a run.
- The GCP and Cloud Run deployment documents (`DEPLOY_GCP*.md`) were removed with the Cloud Run infrastructure; the AWS runbook replaced them.
- Celery, Flower, LangSmith, Groq, Gemini and OpenRouter key rosters, the capacity registry, MSG91 SMS, login OTP, phone sign-in and Intercom: all deleted; see [RULINGS.md](RULINGS.md).

## H. Old product and engineering descriptions (PRD and ESD parts removed 2026-10-14)

Sections of `docs/product/PRD.md` and `docs/architecture/ESD.md` that described the matrix, the PPI framework review gate, retake, the approval state machine, Celery queues, the three-provider model router, Google Cloud deployment, SMS and OTP, and Company DNA were removed in place. The two files now describe current behaviour only; the removed text is recoverable with `git show 73103c3:docs/product/PRD.md` and `git show 73103c3:docs/architecture/ESD.md`.

## I. Pre-reference visual systems (to 2026-10-17)

- **What:** `DESIGN.md` accumulated several incompatible systems: the original R+P monogram sampled from `logo300.jpeg`, blue and teal ramps that did not match the later owner reference, a zero-radius rule, a "text is never grey" rule, and successive Hubot Sans, Mona Sans and DM Serif Display typography experiments.
- **Why obsolete:** the owner supplied a definitive Vivekium HTML palette on 2026-10-17 and requested one current design contract. The product now uses the directional-chevron mark, warm paper palette and the already-approved Fraunces/Outfit typography.
- **Replaced by:** root [../../DESIGN.md](../../DESIGN.md), implemented by `frontend/app/globals.css`, `frontend/tailwind.config.ts` and `frontend/components/brand/logo.tsx`.
- **Original:** `git show 7f6f214:DESIGN.md` retrieves the last pre-reference document.

## J. Demo email allowlist and demo retention sweep (removed 2026-10-31)

- **What:** `DEMO_EMAIL_ALLOWLIST` withheld every recipient not on it (message id `demo-withheld`), and the daily task `pickready.purge_public_demo` (rule `readypick-purge-public-demo`, `DEMO_RETENTION_DAYS`, default 14) deleted `public_demo` tenants and candidate users. Company login was retrieve-only and BD login was refused.
- **Why obsolete:** owner statement of 2026-10-31 (register A-25). The withhold stopped every security code and every notification from reaching a real mailbox, and the sweep would have erased persistent demo accounts and their business records.
- **Replaced by:** [../operations/DEMO_MODE.md](../operations/DEMO_MODE.md) and register A-25.
- **Original:** `git show b222eff4:backend/app/services/demo_access.py` and `git show b222eff4:backend/app/workers/tasks_demo.py`.
