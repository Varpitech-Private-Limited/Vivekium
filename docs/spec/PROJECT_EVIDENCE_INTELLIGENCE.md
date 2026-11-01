# Project Evidence Intelligence

**Status:** current. **Reviewed:** 2026-10-14. Implemented 2026-09-01 from the Project Evidence master brief.

> **Current product phase: original project artifacts are not retained.
> Only derived, structured, versioned Project Evidence Intelligence is
> persisted. Original project retention is a future capability and is out of
> scope for the current implementation.**

## What it is

Candidates may OPTIONALLY add projects to their profile (Validation Profile,
My Profile in the candidate portal): a name, a description of at most 100
words, project files and/or a public repository link. The platform processes
the submission into a structured Project Evidence Record and deletes the
original artifacts. Evidence, not artifacts, is the product.

The governing principle: **claims are signals, projects can provide evidence,
evidence must be interpreted in context.** A submitted project is not
automatically good; an absent project is not automatically bad; no fixed
"project submitted = +N" scoring exists anywhere, and a candidate with no
projects remains fully evaluable through resume, validation profile and
assessment.

"Versioned intelligence" means the evidence is DECOMPOSED into dimensions
(identity, technology stack, architecture, implementation, testing,
infrastructure, documentation, engineering signals, gaps, uncertainties,
provenance), not V1/V2 history of the project.

## Architecture

Everything lives in `backend/app/services/projects/`:

| Module           | Responsibility |
| ---------------- | -------------- |
| `limits.py`      | every ceiling, read once from Settings (`project_*` keys in `core/config.py`) |
| `intake.py`      | upload validation, magic-byte checks, temporary staging under `project-intake/` |
| `formats.py`     | extension classification as data tables: family, language, supported, limitation |
| `archive_safety.py` | ZIP inspection before extraction: traversal, symlinks, bombs, nesting, entry floods |
| `parsers.py`     | deterministic extraction, one `ParsedArtifact` per file, total (never raises on content) |
| `repository.py`  | public-repository ingestion; provider registry keyed by host (GitHub implemented) |
| `evidence.py`    | evidence units (dedupe, rank, cap), the Evidence Record, the reduced AI pack |
| `ai_reasoning.py`| the ONE reasoning call (`project_evidence` task, Terra, temperature 0.0), validated deterministically |
| `pipeline.py`    | lifecycle orchestration, idempotency, verified deletion |
| `context.py`     | consumption: recruiter view (words only) and the AI context block |

Storage: one row per project in `candidate_projects` (migration 0074),
candidate-scoped, JSONB evidence columns, RLS mirroring the `candidates`
policy. No object-store copy of derived evidence is needed; it is compact by
construction. Temporary originals go through the existing shared
`object_storage` S3 transport under the `project-intake/` prefix.

Processing is dispatched (`pickready.process_candidate_project`, on-demand
compute because archive parsing is minutes of work), with an
hourly sweeper (`pickready.reconcile_project_intake`) that retries failed
deletions, re-enqueues lost tasks, and re-attempts missing AI interpretations
(bounded by a run counter).

## The pipeline

    submitted -> processing -> security check (archives inspected before
    extraction) -> deterministic parsing -> evidence units (dedupe, rank,
    cap) -> Evidence Record -> reduced AI pack -> one reasoning call ->
    validation -> persisted -> verified deletion of originals -> processed

Failure states are explicit: `failed_security`, `failed_extraction`,
`partially_processed` (deterministic evidence persisted, AI interpretation
missing; retried automatically and retryable by the candidate). Partial
success is real: one unreadable file never discards the readable ones, and
unsupported formats are recorded on the row with a stated limitation.

### The four data layers, kept apart

1. **Candidate-provided** (name, description): stored verbatim, labelled as
   claims, never rewritten.
2. **Deterministically extracted** (parser signals): reproducible, cited to a
   source path.
3. **Derived evidence** (units, record): built by `evidence.py`, deterministic.
4. **AI interpretation** (claim assessments, synthesis, strength): stored in
   its own column (`ai_interpretation_json`), never merged into the record,
   so a model inference can never read as extracted fact.

### Claims versus observed evidence

The AI receives the candidate description as a CLAIM plus the deterministic
evidence, and returns per-claim assessments from a fixed careful-language
vocabulary: `strongly supported`, `partially supported`, `insufficient
evidence`, `not substantiated by available artifacts`. Language accusing a
candidate of dishonesty is structurally impossible: any label outside the
vocabulary is refused by deterministic validation and the project records
`partially_processed` instead.

`evidence_strength` is a WORD (Strong / Moderate / Limited / Insufficient),
enforced three times: the prompt, `ai_reasoning.validate_interpretation`, and
a database CHECK. No number ever reaches a client, consistent with the
platform-wide rule.

## Security model

Candidate submissions are hostile until proven boring:

- No candidate code is ever executed. No installs, no builds, no shells.
- Ceilings on file count, per-file size, total size, archive entries,
  extraction size, nesting depth, and compression ratio; all configurable in
  Settings, none hardcoded in the pipeline.
- ZIP directories are inspected before extraction; traversal entries,
  symlinks and declared bombs poison the whole archive (`failed_security`).
  Actual read sizes are re-checked during extraction because a central
  directory can lie.
- Magic-byte checks where signatures are known (zip/pdf/docx/xlsx).
- Public repositories only: https, no embedded credentials, provider
  allowlist, tree inspected before any content fetch, generated and
  dependency paths excluded, bounded file count and size.
- Parsers are total: corrupt input yields a recorded limitation, never an
  exception and never hallucinated contents.

Stated limitation, not a guarantee: parsing runs in the worker process, not a
separate sandbox, bounded by the resource ceilings above and the worker's
600 s soft time limit. Malware scanning (ClamAV) is not deployed; the
pipeline never executes or serves the uploaded bytes, and originals are
short-lived by design. Revisit both if original retention ever ships.

## Temporary artifact deletion

Deletion happens ONLY after derived evidence is validated and persisted, and
it is verified: each staged object is deleted and then HEAD-checked. A failed
deletion keeps the key listed in `intake_objects_json`, increments
`deletion_attempts`, logs, and is retried hourly; `original_deleted_at` is
stamped only when nothing remains. There is no fallback archive. An
object-store lifecycle rule on the `project-intake/` prefix is the
recommended backstop for anything a crash orphans (add alongside the existing
bucket lifecycle configuration when infra is next applied).

## AI cost control

Raw projects never reach a model. Reduction is measured and inspectable in
`telemetry_json`: `raw_bytes` (what existed), `evidence_unit_count` (what
deterministic extraction kept after dedupe/rank/cap), `ai_context_chars`
(what the model actually received, capped by `project_max_ai_context_chars`).
One reasoning call per project, on the existing router with the existing
retry/budget/breaker discipline.

## Consumption

- **Candidate portal**: Projects card on My Profile
  (`frontend/components/projects-section.tsx`), endpoints under
  `/portal/me/projects`. Optional, multi-project, status-polled, with
  unsupported-format feedback and the retention notice served by the backend.
- **Recruiter**: `GET /candidates/{id}/project-evidence` behind
  `view_review_screen` (plus the HM-grant gate and a link-in-tenant check;
  cross-tenant answers 404). Rendered as the Projects tab in the review
  screen (`frontend/components/project-evidence-panel.tsx`). The response keeps
  submission status apart from evidence: `projects` is derived evidence for
  evidence-ready rows only; `submissions` lists EVERY submitted project with a
  state word (queued, analysing, ready, summary_only, failed), the
  candidate-safe `status_detail` and timestamps; `counts` totals them. The tab
  reads "3 projects submitted; analysis queued" for pending work and shows the
  failure note for a failed one; "No project evidence submitted" is shown only
  for a candidate with zero submissions, and a failed load is an error, never
  "none". While anything is queued or analysing the tab re-reads every 20
  seconds (visible tab only). That the analysis actually runs is a worker
  concern: see [BACKGROUND_WORK.md](BACKGROUND_WORK.md).
- **AI context**: `services/projects/context.candidate_project_context` joins
  the per-candidate PPI question-generation payload, so the interviewer can
  probe project evidence and its validation areas. It moves no weight, no
  grade, and no report section; the PRISM section order is untouched.

Role relevance is judged at consumption time against the job in context (the
question generator sees the JD beside the evidence); the stored record is
job-agnostic because a project outlives any one application.

## Configuration

All limits are `project_*` settings in `app/core/config.py` with safe
defaults (20 files, 25 MB/file, 100 MB/project, depth 2, 2000 entries,
200 MB extracted, ratio 120, 120 evidence units, 24 000 AI-context chars,
40 repo files at 512 KB each, 10 projects per candidate). `GITHUB_API_TOKEN`
is optional and only raises the public API rate limit; it grants no private
access and no private-repository path exists.

## Testing

`backend/tests/test_project_evidence.py` (pure, no DB): word limit, URL
validation, archive attacks (traversal, symlink, bomb, nesting, floods),
classification, parser signals across domains (Python, package.json, IFC,
STL, corrupt PDF), unit dedupe/cap/provenance, claim separation, absence
gaps, pack ceiling, interpretation vocabulary and no-numbers guards, and the
full lifecycle with fakes: happy path, deletion failure observed and retried,
AI failure as partial success, AI-only completion, hostile archive, vanished
staging, idempotent rerun, discard.

`backend/tests/test_project_evidence_api.py` (live DB, portal pattern):
add/list/delete, endpoint-level word limit, neither-input refusal,
optionality, credentialed-URL refusal, project ceiling.

## Future extension points (documented, NOT implemented)

- Optional original-project retention (enterprise): would hang off the same
  row via a durable object prefix and a retention policy; nothing in the
  current schema forbids it, and nothing in the current code provides it.
- Additional repository providers: a host entry plus a fetcher in
  `repository.py`.
- Additional formats and disciplines: rows in `formats.py` plus a parser in
  `parsers.py` (e.g. IfcOpenShell for deep BIM geometry, Tree-sitter for
  structural source parsing, Docling for rich documents). Rejected for now to
  keep the dependency surface on untrusted input small; the deterministic
  readers cover the evidence signals the product consumes.
- Semantic retrieval over evidence units (embedding into `context_chunks`)
  once a consumer needs cross-project search.
