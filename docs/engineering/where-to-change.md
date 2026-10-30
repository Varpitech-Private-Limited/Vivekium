# Where to make a change

**Status:** current. **Reviewed:** 2026-10-14. The fastest route from "I want to change X" to the right file and the half of the change people forget.

| Adding or changing | Touch | And do not forget |
|---|---|---|
| An API route | `backend/app/api/<section>.py` | `require_capability(...)`, never a role branch; declare every response field; `tests/test_route_callers.py` if no screen calls it |
| A capability | `services/capabilities.py` | A seeding migration too; `tests/test_capability_seed_parity.py`; a removal deletes its `role_permissions` rows in the same change |
| A table | `backend/app/models/`, `backend/alembic/versions/` | RLS policy and grant; revoke UPDATE if insert-only; export from `models/__init__.py` |
| A background task | `workers/tasks.py` or `workers/tasks_*.py` | `@task(name, route, rls)`; dispatch with `dispatch_after_commit`; a real-Postgres test for `rls="tenant"` |
| A scheduled sweep | `workers/schedule.py` and every environment's `module "scheduler"` | `tests/test_schedule_parity.py` compares both |
| Public demo mode (A-23) | `DEMO_MODE`, `NEXT_PUBLIC_DEMO_MODE`, `DEMO_MODE_CONFIRM`, the demo routes, and `pickready.purge_public_demo` in `workers/schedule.py` | Schedule name `readypick-purge-public-demo` in every environment's scheduler; on production the confirm value is `public-demo`. The code default is off. As of 2026-10-30 the pilot API task definition `readypick-pilot-api:55` sets `DEMO_MODE=true` and `DEMO_MODE_CONFIRM=public-demo`. The frontend image `readypick-pilot-frontend:45` (tag `sha-d51149b0b0bf`) was built with `NEXT_PUBLIC_DEMO_MODE=true`. Do not commit `demo_mode = true` in tfvars. [../operations/DEMO_MODE.md](../operations/DEMO_MODE.md) |
| An LLM call | `config/llm_providers.py` first | Task type, timeout, budget, temperature, tokens, retries; register the prompt builder in `tests/test_ctc_never_in_prompt.py` |
| A prompt | `backend/app/prompts/*.txt` | Bump `# version:`; good, bad and edge examples; sufficiency decided first |
| A scoring rule | `services/hiring/runbook_data/*.yaml` | Cite the Runbook section; `test_runbook_parity.py` enforces it |
| What a candidate is assessed against | `services/assessment_contract.py` | Read the snapshot for a started conversation; the digest is logged |
| Job setup, Skills, SWOT, publish | `services/skills.py`, `api/job_setup.py`, `docs/spec/JOB_SETUP_FLOW.md` | The approval flow is decided, not built (J-10 to J-16); do not describe it as working |
| A pipeline stage | `services/hiring_pipeline.py` | The CHECK constraint, `PipelineStatus`, the dashboard mapping, the frontend union |
| A workflow gate | `docs/spec/HIRING_WORKFLOW.md` first | A gate is a real check with tests, not a paragraph |
| A candidate notification | `services/candidate_updates.py` | A fixed catalogue entry, never a prompt; no number; relative link only |
| A client-facing string | The renderer | No number, no em dash, correct names (P-04, P-05) |
| A proctoring threshold or event | `core/config.py` (`proctoring_*`), `services/proctoring/catalog.py`, `phrasing.py` | Served to the browser from `services/proctoring/config.py`; no literal in the pipeline |
| A question format | `services/assessment_formats/types.py` | Payload model, answer model, `candidate_view`, the migration CHECK, the frontend dispatcher |
| Coding questions | `docs/spec/CODING_ASSESSMENT.md` first | Decided design (S-20) differs from the code that exists; read the spec's status table |
| A retrieval change | `services/rag/` | `test_retrieval_tenant_recall.py` measures recall; a degraded reranker is recorded; the injection screen is owed (A-20) |
| A judge or eval change | `backend/app/evaluation/` only | Never under `app/services/`; `test_judge_isolation.py` |
| A tool | `services/tools/permissions.py`, `policy.py` | A bounded READ; the refusal runs before the handler |
| A setting | `backend/app/core/config.py` | `.env.example` or `INTERNAL_ONLY` with a reason |
| A removal | the code, then a sweep test | `tests/removal_sweep.py`; update the owning doc and move the old text to history |
| A frontend surface | `frontend/app/(group)/` | `DESIGN.md` tokens; `can(...)` not roles; `docs/engineering/frontend.md` |
| A candidate-facing upload | the relevant storage service | Validate, never execute, bound every ceiling in config |
| An AI activity message | `services/activity/phrasing.py` | Fixed catalogue keyed by (task, event); never a timer |
| Email sending | `services/email_outbox.py` | One writer; SES-only is decided and not built (I-20) |
| A decision or an open question | `docs/decisions/REGISTER.md`, `OPEN.md` | Date it; mark implemented or pending; update the owning spec in the same change |
