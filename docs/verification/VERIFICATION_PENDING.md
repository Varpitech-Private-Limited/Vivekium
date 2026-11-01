# Verification pending

**Status:** current, read by `test_no_live_vendor_claims.py`. **Reviewed:** 2026-10-14 (the list was rebuilt from the earlier rows and the open items in [../decisions/OPEN.md](../decisions/OPEN.md); nothing here was re-run).

Every claim in this repository that is **not proven**, and the command or event that would settle each one. What is proven, with dates and figures, is [VERIFICATION_RESULTS.md](VERIFICATION_RESULTS.md): the reasoning, extraction and embedding paths and the credential-failure and timeout behaviour were run against the live vendors on 2026-08-31 and again for the reranker on 2026-09-09.

For every path **not** recorded in `VERIFICATION_RESULTS.md`, the honest framing is: **built and tested against recorded fixtures and a stub provider; not executed against a live provider.**

This file is append-only in practice. Add a row when you write code whose correctness depends on something you could not observe. Delete a row only when the command in it has been run and its result recorded in `VERIFICATION_RESULTS.md`. A row whose command fails stays.

## How to run the vendor checks

1. Export `OPENAI_GPT_TERRA`, `OPENAI_GPT_LUNA`, `VOYAGE_CONTEXT_4` (and `VOYAGE_RERANK_2_5` for the reranker).
2. `bash -c 'set -a; . .env; set +a; python scripts/verify_live.py'`, or one path with `--only <name>`.
3. Record every outcome, pass or fail, with the figures it printed. Re-run after any change to the transport, the model ids or the credentials.

## Outstanding claims

| # | Claim that is unproven | Owner | Settled by |
|---:|---|---|---|
| 1 | A real 429 from either vendor carries a `retry-after` header in seconds, and the router prefers it over its own backoff. Asserted against recorded fixtures only; the 429 path has never been provoked live. | `app/services/llm_router.py` | A provoked 429 under `scripts/verify_live.py` |
| 2 | An invalid credential returns 401 or 403, not another status. The first-occurrence breaker rule rests on it. | `llm_router.py` | `verify_live.py --only credential_failure` (a pass is recorded for the current ids; re-run after any vendor change) |
| 3 | The interactive caps are achievable: 15s per attempt and 30s total for short tasks, 25s and 50s for `jd_generation` and Save Skills' context call. Reasoned, not measured on these models. | `app/config/llm_providers.py` | A latency run on pilot |
| 4 | Per-million token prices in `TOKEN_PRICES_USD_PER_MILLION` match an invoice. No price sheet was reconciled; the rows encode the ratio between tiers. | `app/config/llm_providers.py` | Compare `estimate_cost_usd` with real `usage` and an invoice |
| 5 | Both models report `usage.prompt_tokens_details.cached_tokens`; reordering a prompt produces a cache hit; a cached-input rate exists on file (none does, so a hit currently produces no saving in any reported figure). As last recorded (2026-09-22). | `services/prompt_cache`, `cost_telemetry` | A real run read back from `assessment_cost_records` |
| 6 | `assessment_cost_records` (migration 0109) has been written in a deployed environment. As last recorded. | `services/cost_telemetry` | A row after a real assessment on pilot |
| 7 | Retrieval quality: the golden set is 60 of 300 cases, none human verified; the only run is a reference fixture. | `app/evaluation/` | A human-labelled set and a recorded run |
| 8 | The object-store reconciliation finds no orphan in either direction. The bucket half has never run where `S3_BUCKET` was empty. | `services/` storage reconcilers | A run against pilot's bucket |
| 9 | A real two-segment session recording joins correctly (the concat step). | `services/video` | The first real two-segment recording on pilot |
| 10 | Spoken-answer transcription and inbound reply email work in deployment. Observed 2026-11-01 (read-only): the task-worker Lambda has `TRANSCRIBE_ENABLED=true`, region ap-south-1 and the working bucket, but the API task definition has no `TRANSCRIBE_ENABLED`, so the microphone is never offered; and the Lambda grant allowed only `readypick-*` job names while the code named jobs `voice-*` (fixed in code, not yet deployed). No spoken answer has run end to end. | `transcribe_enabled`, `INBOUND_EMAIL_DOMAIN` | Enable, then observe a real answer and a real reply |
| 11 | The CSP admits the Razorpay checkout and the Google sign-in popup; no secret still holds `PLACEHOLDER_NOT_CONFIGURED`; rate limits hold under load. | `frontend/`, `infra/` | A browser run of each flow; a secrets audit; a load test |
| 12 | Restoring the database works within the stated target. The runbook was written, never rehearsed. | [../operations/DISASTER_RECOVERY.md](../operations/DISASTER_RECOVERY.md) | A rehearsal |
| 13 | Decided and not built, so unproven by definition: the approval flow, the static-checker coding path, SES-only email, the retrieval injection screen. | register Pending changes | Build, then the tests named in the register |

## Standing language rule

Nowhere in this repository, in a commit message, in `CLAUDE.md` or in a report, may any wording imply a live vendor call has succeeded unless `VERIFICATION_RESULTS.md` records it. In particular the phrases **"verified against the API"**, **"confirmed working"** and **"tested live"** are forbidden for work that has no such record. The correct phrasing is "built and tested against recorded fixtures and a stub provider; not executed against a live provider".
