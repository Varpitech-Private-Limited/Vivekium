# Vivekium documentation

**Status:** current. **Reviewed:** 2026-10-14. The index of every document. Read the row you need, not the library: each page links to the next one it depends on.

Six files stay at the repository root because a tool or a convention resolves them there: [`README.md`](../README.md) (entry point), [`CLAUDE.md`](../CLAUDE.md) and [`AGENTS.md`](../AGENTS.md) (agent instructions), [`CONTRIBUTING.md`](../CONTRIBUTING.md), and [`PRODUCT.md`](../PRODUCT.md) with [`DESIGN.md`](../DESIGN.md) (the Impeccable design tooling reads them from the root). Path-scoped agent rules are in `.claude/rules/`.

## Start here

| I want to... | Read |
|---|---|
| Understand what the product does | [product/PRD.md](product/PRD.md) |
| Understand how it is built | [architecture/ESD.md](architecture/ESD.md) |
| Know what is decided, what is owed, what is open | [decisions/REGISTER.md](decisions/REGISTER.md), [decisions/OPEN.md](decisions/OPEN.md) |
| Change code without breaking a rule | [../CLAUDE.md](../CLAUDE.md), then the page in [engineering/](engineering/) for the area |
| Find where a change goes | [engineering/where-to-change.md](engineering/where-to-change.md) |
| Run it locally and test it | [../CONTRIBUTING.md](../CONTRIBUTING.md), [operations/SETUP.md](operations/SETUP.md) |
| Deploy it, or get the data back | [operations/DEPLOY_AWS.md](operations/DEPLOY_AWS.md), [operations/DISASTER_RECOVERY.md](operations/DISASTER_RECOVERY.md) |
| Know what runs where on AWS | [operations/INFRA_TOPOLOGY.md](operations/INFRA_TOPOLOGY.md) |
| Know who may do what | [spec/RBAC_SPECIFICATION.md](spec/RBAC_SPECIFICATION.md), [reference/RBAC.md](reference/RBAC.md) |
| Know how candidates are evaluated | [product/Readypick Hiring Philosophy.md](product/Readypick%20Hiring%20Philosophy.md) |
| Set up a job: JD, Skills, approval, publish, the freeze | [spec/JOB_SETUP_FLOW.md](spec/JOB_SETUP_FLOW.md) |
| Follow an assessment from invitation to PRISM Report | [spec/ASSESSMENT_FLOW.md](spec/ASSESSMENT_FLOW.md) |
| Follow a candidate or a job end to end | [spec/HIRING_WORKFLOW.md](spec/HIRING_WORKFLOW.md) |
| Add, route or debug a background task | [spec/BACKGROUND_WORK.md](spec/BACKGROUND_WORK.md) |
| Prove a change works end to end | [spec/HARNESS.md](spec/HARNESS.md) |
| Keep disk, worktrees and Docker clean | [development-storage-policy.md](development-storage-policy.md) |
| Learn why something is the way it is | [history/README.md](history/README.md) |

## Precedence, when two documents disagree

Details in [engineering/documentation-policy.md](engineering/documentation-policy.md). In short: (1) the latest dated owner statement in the decision register; (2) the RBAC specification, as amended; (3) the Runbook; (4) the specifications in `spec/`; (5) implementation documents. Code and tests show what exists, not what was intended. An unanswered question is never approved.

## The map

### `product/` and `decisions/`: what the product is and what is decided

| File | Holds |
|---|---|
| [product/PRD.md](product/PRD.md) | Product requirements, current behaviour, with decided-pending sections labelled |
| [product/Readypick Hiring Philosophy.md](product/Readypick%20Hiring%20Philosophy.md) | The Runbook, RPN-PHIL-001 v1.4. Owner-filed. Loaded by code at this path |
| [decisions/REGISTER.md](decisions/REGISTER.md) | Every decision in force, with status and the Pending changes table |
| [decisions/OPEN.md](decisions/OPEN.md) | Unresolved questions, accepted debt and contradictions found in audits |

### `architecture/`: how it is built

| File | Holds |
|---|---|
| [ESD.md](architecture/ESD.md) | Engineering and system design: the map, the invariants, where each concern lives |
| [AI_RUNTIME.md](architecture/AI_RUNTIME.md) | The AI runtime as built: retrieval, tool layer, scoring lock, evaluation, and what is not built |
| [adr/](architecture/adr/) | Architecture decision records |

### `spec/`: normative specifications

| File | Holds |
|---|---|
| [RBAC_SPECIFICATION.md](spec/RBAC_SPECIFICATION.md) | Rank 1 (owner-filed, amended by the register): roles, capabilities, isolation, lifecycle |
| [HIRING_WORKFLOW.md](spec/HIRING_WORKFLOW.md) | The candidate and client journeys and the eight gates |
| [JOB_SETUP_FLOW.md](spec/JOB_SETUP_FLOW.md) | JD, Skills, Save Skills, the Final Job Posting, publish, the freeze; section 7 is the decided approval flow |
| [ASSESSMENT_FLOW.md](spec/ASSESSMENT_FLOW.md) | Invitation to PRISM Report: question budget, the start, the server clock, scoring stages, Miti and Siddhi interfaces |
| [ASSESSMENT_QUESTION_FORMATS.md](spec/ASSESSMENT_QUESTION_FORMATS.md) | The six question formats, scoring and the candidate boundary |
| [CODING_ASSESSMENT.md](spec/CODING_ASSESSMENT.md) | Coding questions without a sandbox: decided, not built |
| [PROCTORING.md](spec/PROCTORING.md) | Mandatory monitoring: principles, paths including the device pause, audio rules, the report, retention |
| [RETRIEVAL.md](spec/RETRIEVAL.md) | Candidate Retrieval (Yukti) and Evidence RAG: ownership and boundaries |
| [CANDIDATE_COMMUNICATIONS.md](spec/CANDIDATE_COMMUNICATIONS.md) | Candidate identity, the idle rule, the application form, messages, the one email writer |
| [CANDIDATE_DASHBOARD_SPECIFICATION.md](spec/CANDIDATE_DASHBOARD_SPECIFICATION.md) | The candidate list surface (owner-filed, rank 4) |
| [PROJECT_EVIDENCE_INTELLIGENCE.md](spec/PROJECT_EVIDENCE_INTELLIGENCE.md) | Project evidence: pipeline, security, retention |
| [BACKGROUND_WORK.md](spec/BACKGROUND_WORK.md) | Dispatch, routing, retries and schedules |
| [HARNESS.md](spec/HARNESS.md) | RPN-HARNESS-001: scenarios, fault injection, replay, baselines |
| [ARCHITECTURE.md](spec/ARCHITECTURE.md) | Normative architecture decisions (AD-1: the database is authoritative) |

### `engineering/`: how to change the code

| File | Holds |
|---|---|
| [where-to-change.md](engineering/where-to-change.md) | "I want to change X" to the right file and the forgotten half |
| [backend.md](engineering/backend.md), [database.md](engineering/database.md), [security.md](engineering/security.md), [ai-runtime.md](engineering/ai-runtime.md), [frontend.md](engineering/frontend.md), [testing.md](engineering/testing.md), [infrastructure.md](engineering/infrastructure.md) | The rules per area |
| [documentation-policy.md](engineering/documentation-policy.md) | Status labels, precedence, lifecycle, checks |
| [agent-workflow.md](engineering/agent-workflow.md) | Context management, handoff, compaction, the extension audit |

### `reference/`: lookup material

| File | Holds |
|---|---|
| [RBAC.md](reference/RBAC.md) | The implemented capability matrix |
| [JOB_SETUP_API.md](reference/JOB_SETUP_API.md) | The job setup API shapes the frontend builds against |

### `operations/`: running it

| File | Holds |
|---|---|
| [SETUP.md](operations/SETUP.md) | File map, one-time CI/CD setup, what is not obvious |
| [DEPLOY_AWS.md](operations/DEPLOY_AWS.md) | The AWS deployment runbook |
| [INFRA_TOPOLOGY.md](operations/INFRA_TOPOLOGY.md) | What runs where on pilot: compute, data, object store, network, schedules, alarms, switches |
| [DEMO_MODE.md](operations/DEMO_MODE.md) | Public demo mode: flags, confirm gate, who is refused, credits, email allowlist, cleanup. Default off. Not a live activation |
| [DISASTER_RECOVERY.md](operations/DISASTER_RECOVERY.md) | Restoring the database; written, never rehearsed |
| [BRANCH_PROTECTION.md](operations/BRANCH_PROTECTION.md) | The protection on `main` (applied) and its ten required checks |
| [LEGACY_TABLES.md](operations/LEGACY_TABLES.md) | Every table and column dropped or kept as history, with the reason |
| [SKIPS.md](operations/SKIPS.md) | The declared skip inventory, enforced by a test |
| [development-storage-policy.md](development-storage-policy.md) | Permanent workspace hygiene policy |

### `verification/`: what has been proven

| File | Holds |
|---|---|
| [VERIFICATION_RESULTS.md](verification/VERIFICATION_RESULTS.md) | Live vendor runs that succeeded, with dates |
| [VERIFICATION_PENDING.md](verification/VERIFICATION_PENDING.md) | What remains unproven, stated plainly |
| [AI_UPGRADE_BASELINE.md](verification/AI_UPGRADE_BASELINE.md) | The 2026-09-09 measurement later work is compared against; never edited to match new behaviour |

### `history/`: provenance, never instructions

[history/README.md](history/README.md) indexes the records and the old paths. Do not read a history file as a description of how the product works today.

## Documents that tests or code resolve on disk

Moving or rewording one breaks a test or a service; the table is [engineering/testing.md](engineering/testing.md#documents-that-tests-read). Also: `.impeccable-exceptions.md` (root) is read by `frontend/scripts/impeccable-gate.mjs`, and the Runbook's filename has spaces, a known wart kept because renaming it would touch 103 citations.
