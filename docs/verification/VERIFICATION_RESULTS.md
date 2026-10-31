# Verification results
**Status:** measurement: a dated record of live vendor runs, read by `test_no_live_vendor_claims.py`; never edited to match new behaviour. **Reviewed:** 2026-10-17.

Produced by `backend/scripts/verify_live.py` against the live vendor
endpoints. Everything below is an OBSERVED result; nothing here is inferred
from documentation.

- Run at: 2026-08-31 16:36:50Z
- Commit: `b87a5aa53c587bdd605ce05391593c8414bdb239`
- Reasoning path: `gpt-5.6-terra`
- Extraction path: `gpt-5.6-luna`
- Embedding path: `voyage-4`

Every path exercised returned the shape this codebase was built against.

| Path | Vendor | Model | Result | ms | Detail |
|---|---|---|---:|---:|---|
| `reasoning` | OpenAI | `gpt-5.6-terra` | PASS | 5711 | 1037 characters of text returned |
| `extraction` | OpenAI | `gpt-5.6-luna` | PASS | 4848 | top-level object with keys ['comments', 'education', 'experience', 'role_fit', 'skills'] |
| `embedding` | Voyage | `voyage-4` | PASS | 1219 | 2 vectors, 1024 wide |
| `credential_failure` | OpenAI | `gpt-5.6-luna` | PASS | 973 | 401 classified as credential, breaker trips on first |
| `timeout` | OpenAI | `gpt-5.6-terra` | PASS | 1825 | raised inside the budget: openai exhausted for task_type=report_synthesis: transport (ConnectTimeout); transport (ConnectTimeout); transport (ConnectTimeout) |
| `rate_limit` | OpenAI | `gpt-5.6-luna` | NOT PROVOKED | 5531 | the call succeeded, so no rate limit was reached. The classifier and the retry-after reader remain proven only against recorded fixtures. |

## What a NOT PROVOKED row means

The branch could not be reached without abusing the vendor. It is not a pass
and it is not a failure: it means the behaviour remains proven only against the
hand-authored fixtures in `backend/tests/fixtures/vendor/`.

## Next

Remove the corresponding rows from `VERIFICATION_PENDING.md` **only** for the
paths that show PASS above. A row is removed by a run that succeeded, never by
a run that was attempted.

---

# The reranker, live. RPN-AI-UP-001 W6.1 (2026-09-09)

Until today `claude.md` carried this under "what is NOT proven": *"No live
rerank call has ever been made. `rerank-2.5` has not been resolved against the
endpoint; the module is proven against a fake shaped like the installed SDK."*
It has now been made, twice: once against the raw endpoint to resolve the model
id, and once through the SHIPPED module so the installed SDK's real response
shape is what `_voyage_order` reads.

- Run at: 2026-09-09
- Credential: `VOYAGE_RERANK_2_5`, added to the environment on this date. It
  holds the same Voyage ACCOUNT key as `VOYAGE_CONTEXT_4`, because one Voyage
  account serves both `/v1/embeddings` and `/v1/rerank`. The two names are kept
  separate deliberately, per the convention that a credential is named after the
  model it unlocks, so an absent key names the missing CAPABILITY.

| Path | Vendor | Model | Result | Detail |
|---|---|---|---:|---|
| `rerank_model_id` | Voyage | `rerank-2.5` | PASS | 200, `usage.total_tokens` 28. The vendor's own supported list, returned verbatim in a 400 for a bad id, is `['rerank-lite-1', 'rerank-2-lite', 'rerank-2', 'rerank-3', 'rerank-3-lite', 'rerank-2.5', 'rerank-2.5-lite']` |
| `rerank_sdk_shape` | Voyage | `rerank-2.5` | PASS | through `services/rag/reranker.rerank_chunks`; the run recorded `reranker="voyage", degraded=False` |
| `rerank_moves_the_order` | Voyage | `rerank-2.5` | PASS | over four chunks whose FUSED order put an irrelevant retail chunk first at 0.9, the cross-encoder returned both Kafka chunks (fused 0.5 and 0.4) above it |
| `rerank_degradation_recorded` | Voyage | n/a | PASS | with the credential blanked, the run recorded `reranker="lexical", degraded=True, reason="credential_not_configured"` and returned results rather than raising |

**`rerank-3` remains refused and the reason is unchanged.** It is Preview, and
the module's docstring argues a preview model has no place under a
grade-adjacent pipeline. This run does not disturb that: it confirms
`rerank-2.5` is real, which is the only thing that was in doubt.

---

# The judge determinism probe. RPN-AI-UP-001 W7.2 (2026-09-09), PARTIAL

**This is not a completed measurement and must not be cited as one.** The full
probe is 3 models x 2 arms x 5 cases x 20 repeats = 600 calls, and the Gemini
free tier's daily allowance was exhausted before it finished. Every key answered
429 on every model at the end of the session.

What the run DID establish, and each of these is worth more than the sigma would
have been:

| Finding | Detail |
|---|---|
| `models.list` is not a capability list | The account's model listing advertises `gemini-2.5-pro`, `gemini-2.5-flash` and `gemini-2.5-flash-lite`. All three answer `generateContent` with 404 "no longer available to new users". This is the `voyage-context-4` failure in a new shape and it is why `JUDGE_MODELS` is now resolved by CALL, never by listing. |
| The panel that answers | `gemini-3.5-flash`, `gemini-3.6-flash`, `gemini-3.7-flash` all returned 200. Every `-pro` id answers 429 on these keys, so no pro juror exists to seat. |
| The keys are separate meters | Key 1 answered 200 in the same second keys 2 and 3 answered 429, so the slots are separate projects, and rotation is real throughput rather than theatre. |
| A quota rejection is not dispersion | Recorded because getting it wrong was the probe's first result: it reported `sigma=0.0000, unanimous 5/5` out of five cases of nothing but 404s. Only in-scale answers now count toward the rate, `usable_share` travels beside every sigma, and an arm that measured nothing reports `unavailable`. |
| Where verdicts WERE obtained, they were unanimous | `gemini-3.5-flash` `clear-strong`: 8 usable calls, all `highly_matching`. `gemini-3.7-flash` `clear-strong`: 6 usable, all `highly_matching`; `boundary-thin`: 3 usable, all `moderately_matching`. **Suggestive only.** Eight calls is not twenty, and the cases that survived the quota are the EASY ones, which is exactly the sampling bias the probe's own docstring warns produces a reassuring number. |

**Consequence, stated plainly:** `configured_jurors()` still returns an empty
tuple, and W8's release gate still has no measured threshold to rest on. The
transport (`app/evaluation/judges/gemini.py`) and the probe
(`app/evaluation/judges/determinism.py`) are written, exercised and correct;
what is missing is quota, not code. Completing it needs either a paid Gemini
tier or a rerun after the daily allowance resets:

    python -m app.scripts.probe_judge_determinism --repeats 20 --json

Seating a jury on the partial numbers above would produce a kappa describing
nothing while looking exactly like a measurement, which is the failure the whole
package was built to prevent.

---

# The deployment, and what was verified IN PRODUCTION (2026-09-09)

Commit `f1416e4` on `feat/ai-upgrade-rpn-ai-up-001`. Backend image
`sha-b790534`, index digest
`sha256:a5003fc033059401c9ba4d921c93c1d98c1af6afb390575615eaf456d8dc7b37`,
arm64 confirmed from the manifest rather than assumed.

| Step | Result |
|---|---|
| Backend suite on the deployed commit | 6071 passed, 1 skipped, 0 failed |
| `terraform apply` (pilot) | 10 added, 12 changed, 5 replaced. No standalone destroy: the five are task-definition revisions |
| Migration job | exit 0, polled to STOPPED. `aws ecs run-task` returning is not the migration finishing |
| Services rolled | api 24->25, frontend 13->14, analysis 11->12 |
| Lambdas | 3 image-backed functions updated by DIGEST |
| **Verified by digest** | api, frontend and analysis: EVERY running task is the image this build produced |
| Production read-back (`pilot-baseline.sh`) | `schema_version: 0092_security_provenance`, `context_chunks_total: 6`, `context_chunks_embedded: 6` |
| New schedules | `readypick-release-held-assessments` and `readypick-sync-intercom-companies`, both ENABLED |

## The reranker, exercised IN THE CLUSTER

A one-shot Fargate task on the `api` task definition, so it ran under the api
task role with the real Secrets Manager value, not a local `.env`:

    BACKEND: voyage
    CRED_PRESENT: True
    CRED_IS_PLACEHOLDER: False
    RECORD: {'reranker': 'voyage', 'degraded': False, 'reason': None}
      fused=0.5  Led the Kafka migration and tuned consumer group r
      fused=0.4  Debugged partition assignment stalls in a multi-br
      fused=0.9  Managed a retail store team and handled inventory

`CRED_IS_PLACEHOLDER: False` is checked explicitly and is not a formality. The
secrets module seeds every container with a placeholder VERSION so tasks can
start, and this platform has already shipped a release where
`FIREBASE_SERVICE_ACCOUNT_JSON` held `PLACEHOLDER_NOT_CONFIGURED` in a
perfectly healthy-looking secret: a secret CONTAINER is not a configured
secret.

The ordering is the finding. The cross-encoder put both Kafka chunks above the
retail chunk that FUSION had ranked first at 0.9, so what ran was a real
reranking rather than a pass-through returning the input order.

## Two things this deployment found

**A Lambda will not accept an OCI index carrying an attestation manifest.**
`docker buildx --provenance=true` pushes an index whose children are the image
manifest and a provenance attestation. ECS pulled it without complaint; Lambda
answered `InvalidParameterValueException: The image manifest, config or layer
media type for the source image ... is not supported`. The functions are
pointed at the CHILD arm64 manifest
(`sha256:218a11d72340a77123c95658157fcbc9586d7614be040b211d7279e16b524583`)
rather than at the index. Worth knowing before the next manual deploy: the two
runtimes do not accept the same artifact reference.

**`rds.force_ssl` is a STATIC parameter and the module never said so.**
Terraform defaults `apply_method` to `immediate`, RDS refuses `immediate` for a
static parameter, and `plan` accepts it happily. The failure only appears on
the SECOND apply, once the parameter group already carries `pending-reboot` in
its deployed state, which is the first apply this branch reached. Fixed in
`f1416e4`; the value is unchanged at "1".

## One untidy artifact, recorded rather than hidden

The tag `sha-f1416e4` on the backend repository points at index digest
`sha256:c0754de5...`, NOT at the deployed `sha256:a5003fc0...`. It was created
by re-registering a manifest that had been round-tripped through
`--output text`, which reserialised it into different bytes. Nothing is
deployed from it. It was left in place rather than deleted because its child
manifests are shared with the deployed index. **Deploy from `sha-b790534`.**

---

# W7.2 COMPLETE. The judge determinism measurement (2026-09-09)

Superseding the PARTIAL Gemini entry above. Gemini's free tier exhausted its
daily allowance at roughly a third of 600 calls; the Groq keys already in the
environment completed the run.

    python -m app.scripts.probe_judge_determinism --vendor groq --repeats 20

Six arms, five cases each, twenty calls per case. `usable_share` was 1.00 on
every arm, so every number below rests on twenty real verdicts.

| model | arm | pooled sigma | worst case | unanimous |
|---|---|---:|---:|---|
| `qwen/qwen3.8-27b` | temperature 0 | 0.0000 | 0.000 | 5/5 |
| `qwen/qwen3.8-27b` | temperature 0 + seed | 0.0000 | 0.000 | 5/5 |
| `openai/gpt-oss-120b` | temperature 0 | 0.0300 | 0.150 | 4/5 |
| `openai/gpt-oss-120b` | temperature 0 + seed | 0.0000 | 0.000 | 5/5 |
| `openai/gpt-oss-20b` | temperature 0 | 0.0200 | 0.100 | 4/5 |
| `openai/gpt-oss-20b` | temperature 0 + seed | 0.0100 | 0.100 | 4/5 |

## What it settled

**A SEED IS NOT DETERMINISM.** It helps and it does not guarantee.
`gpt-oss-120b` went 0.0300 to 0.0000 with a seed; `gpt-oss-20b` still disagreed
with itself at 0.0100 WITH one. So W7.2's central question is answered against
the seed: reproducibility rests on REPEATS WITH REPORTED DISPERSION. That
matches what this platform already found for its own models, where
`temperature=0.0` is refused outright and `system_fingerprint` came back null.

**DISPERSION LIVES AT BAND BOUNDARIES.** Every non-zero cell is a boundary case.
`clear-strong`, `clear-absent` and `claim-without-detail` were 20 for 20 on
every model in every arm. A probe made of obvious cases would have reported
0.0000 across the board and calibrated the gate on the wrong distribution.

**THE PANEL IS STEADIER THAN ITS MEMBERS.** The worst single juror moved on 3 of
20 calls for one case. A majority over three needs two to move together before
the pooled label does.

**IT DID NOT SETTLE JUDGE QUALITY.** Self-agreement is not accuracy: a model
answering `matching` every time agrees with itself perfectly. The reasoning and
decision sets stay EMPTY until a human labels them.

## What it unblocked

`app/evaluation/release_gate.py` (W8) now exists with a threshold derived from
the measurement rather than guessed: `NOISE_BAND = 0.03 x 3 = 0.09`. A fall in
agreement inside that band is reported and does not fail, because it is the
judge disagreeing with itself. A fall wider than it fails even when the absolute
value still clears the floor.

**UNAVAILABLE IS NOT A PASS.** With the human-labelled sets empty the gate
returns `unavailable` and `releasable=False`. A metric that could not be
computed must block, or the first thing a broken harness does is wave every
release through while showing green.

## The jury pipeline, proven end to end (W7.4, W7.5)

`python -m app.scripts.probe_judge_jury` against the real panel: MCC 0.627,
Cohen's kappa 0.556, raw agreement 0.667 reported only beside the kappa with its
38.6-point caveat, full confusion matrix, accuracy interval, 6 presented, 6
judged, 0 abstentions.

**THOSE CASES ARE SYNTHETIC AND LIVE IN THE SCRIPT.** They prove the plumbing:
a real panel, real calls, majority pooling with ties abstaining, and
`build_result` producing chance-corrected metrics. They are not evidence about
candidates, and they are deliberately not in `app/evaluation/datasets/`, where
they would be indistinguishable from human labels in six months.

## Two transport findings

**Groq sits behind Cloudflare, which answers 403 code 1010 to urllib's default
User-Agent.** Not a credential failure and not a rate limit. Without the header
every call fails looking like a rejected key.

**A fault is classified by STATUS as well as by message text.** Groq's 429 body
matched no message fragment, read as `unclassified`, and was therefore never
retried, because `unclassified` is not in `RETRYABLE_FAULTS`.

## One juror excluded for a reason worth recording

`qwen/qwen3.6-27b` ANSWERS CORRECTLY and is still not on the panel. It emits a
`<think>` scratchpad on every call, spends its whole token ceiling on it, then
meets the per-minute meter: it made no measurable progress in eleven minutes
while the other models finished cases in seconds. A juror that cannot be
measured inside a probe's budget cannot inform a gate threshold.
`strip_reasoning` is kept anyway, because any model may emit one.

---

# Second deployment, commit 418b1c3 (2026-09-09)

Backend image `sha-418b1c3`, digest
`sha256:fbc7e572be84acea6ec73d7afa1ac1e0d83f8c3f228ba9ece7a550ddcd6ea925`.

| Step | Result |
|---|---|
| Backend suite on the deployed commit | 6093 passed, 1 skipped, 0 failed |
| `terraform apply` | 3 added, 2 changed, 3 replaced. The three replacements are task-definition revisions |
| Migration job | exit 0, polled to STOPPED |
| Services rolled | api, frontend, analysis |
| Lambdas | 3 image-backed functions, updated BY DIGEST |
| **Verified by digest** | every running task is this build |
| Production read-back | `schema_version: 0092_security_provenance` |
| Site | 200 |
| API errors in the ten minutes after rollout | none |

## The Lambda manifest problem is solved, not worked around

The previous deployment could not point a Lambda at the image it had built:
`docker buildx --provenance=true` pushes an OCI INDEX whose children are the
image manifest and a provenance attestation, ECS pulls it happily, and Lambda
answers `InvalidParameterValueException: The image manifest, config or layer
media type for the source image ... is not supported`. That deploy pointed the
functions at the child arm64 manifest instead, which worked and left two
different digests describing one build.

This build uses `--provenance=false --sbom=false`, which pushes a single plain
manifest. ECS and Lambda now accept THE SAME digest, so "verify by digest" means
one number for the whole deployment rather than one per runtime.

---

# W6.3 asymmetric embeddings: already wired, now proven live (2026-09-10)

The workstream brief allowed for the possibility that this was already done,
and it was. `rag/index.py` embeds with the DOCUMENT input type,
`rag/retrieval._semantic` calls `embed_query` and carries a comment saying
exactly why, and `matching.py` is deliberately symmetric because its stored
vectors are compared in both directions (a job ranked for a resume and a
resume ranked for a job read the same columns). No code changed for this
workstream; what was missing was live evidence that the asymmetry is real.

- Run at: 2026-09-10, `VOYAGE_CONTEXT_4`, model `voyage-4`

| Path | Result | Detail |
|---|---|---|
| `embed(text, input_type="document")` vs `embed(text, input_type="query")` | PASS | the SAME sentence produced two different 1024-wide vectors: cosine similarity 0.841414 between them, all 1024 components differing |

A cosine of 0.84 between two embeddings of one identical sentence is the
vendor's query/document asymmetry doing real work. Had the two come back
identical, `embed_query` would have been decoration and the retrieval comment
a false claim.

---

# The retrieval golden set: 60 cases, version 2026.Q3.2 (2026-09-10)

Q3.1's 24 hand-authored queries and 60 chunks are frozen and carried verbatim;
36 new hand-authored queries over 90 new hand-authored chunks join them across
six new role domains. All synthetic by construction, which W7.1 permits for
retrieval because a query-to-chunk pair is objectively checkable.

| Check | Result |
|---|---|
| `python -m app.scripts.eval_retrieval --gate` | exit 0 |
| Harness self check | passed, 4 stamped values reproduced within 1e-09 |
| Case count against the floor | 60 of 300, reported by the gate itself |
| Human verification | 0 of 60; every case carries `human_verified: false` |
| Quality gate eligibility | still refused: the only run is a `reference_fixture` |

**What this did NOT change, stated so nobody infers otherwise:** retrieval
QUALITY remains unmeasured. The fixture run measures the metric harness, not
the retriever; quality becomes measurable only with a `recorded` run against
an index holding real volume, and the deployed environment still holds zero
candidates. The reasoning and decision sets remain EMPTY at version 2026.Q3.2
and must stay empty until a human labels them.

---

# Third deployment, commit 3b27abb (2026-09-10)

Native support replaces the deleted vendor sync; the pilot database grows up a
size; reports gain provenance columns. Backend image `sha-3b27abb`, digest
`sha256:bfa2a8eb48fa793107cd9814e03c08fc7b11520ba2f9f600206a26c5ab7e86dd`;
frontend image `sha-3b27abb`, digest
`sha256:e54c7d134918d7b8150a5991d8f97e99f4dcf546de517b5bc22814fb11ae5f70`.
Single plain manifests (`--provenance=false --sbom=false`), so ECS and Lambda
share one digest per image.

| Step | Result |
|---|---|
| Backend suite on the deployed commit | 6132 passed, 1 skipped, 0 failed |
| `terraform apply` (pilot) | 4 added, 4 changed, 5 destroyed: four task-definition revisions, the RDS in-place modify, and the vendor sync's EventBridge rule destroyed |
| RDS after the apply | `db.t4g.medium`, status available, `PendingModifiedValues` empty, `max_allocated_storage` 200. Read back from `describe-db-instances`, not from the plan |
| RDS Proxy | NOT built, owner decision, after the vendor's pinning documentation was read: for PostgreSQL the proxy pins on SET commands, `set_config()`, and named prepared statements, and this application does all three on effectively every session. The reasoning lives beside the `instance_class` line in `infra/environments/pilot/main.tf` |
| Migration job | exit 0, polled to STOPPED; schema read back as `0094_report_provenance` |
| Services rolled | api 26 to 27, frontend 14 to 15, analysis already on 12 |
| Lambdas | all 3 image-backed functions running `sha-3b27abb` |
| **Verified by digest** | api (4 tasks), frontend (2), analysis (4): every running task is the image this build produced |
| Support routes live | `GET /api/v1/support/threads` and `GET /api/v1/provider/support/threads` both answer 401 unauthenticated: mounted and gated, not 404 |
| Vendor sync rule | `readypick-sync-intercom-companies` absent from the scheduler listing |
| Site | 200 |
| API errors in the ten minutes after rollout | none |

## What this deployment deliberately did not prove

The Support surface is proven live at the ROUTE level (mounted, auth-gated,
zero errors) and end to end in the suite (RLS in both directions, the FSM,
the notification dispatch, the capability gates, against a real database).
No support thread has been opened through the production UI yet, because the
environment's three tenants are demo tenants and opening one is a signed-in
human act. The first real thread is the remaining live exercise, and it is a
two-minute manual step, not an engineering gap.

The RDS bump is proven applied; what it is FOR (HNSW working memory, the
connection ceiling under Lambda concurrency) becomes measurable only when
real load exists. The standing watch item is CloudWatch `DatabaseConnections`
against the new ceiling, and the pinning analysis stands recorded for whoever
next reaches for a proxy.

---

# Fourth deployment, commit bf74fc1: the audit close-out (2026-09-11)

The LLD engineering audit's three fixes, deployed and proven. Backend image
`sha-bf74fc1`, digest
`sha256:0fe4cdda9e9a90fed0291bada753f760afffb9f9806370360e91cef832a891a5`;
frontend unchanged and deliberately redeployed from yesterday's
`sha-3b27abb` digest, because no frontend file changed and a rebuilt
identical image would only manufacture a second digest for one artifact.

| Step | Result |
|---|---|
| Backend suite on the deployed commit | 6135 passed, 1 skipped, 0 failed |
| `terraform apply` (pilot) | 3 added, 5 changed, 3 destroyed: three backend task-definition revisions and the three autoscaling minimums lowered to one |
| Migration job | exit 0, polled to STOPPED; schema read back `0094_report_provenance` (no new migration this release, run per procedure) |
| Lambdas | all 3 image-backed functions running `sha-bf74fc1` |
| **Verified by digest** | api, frontend, analysis: every running task is the expected image |
| **The resize, proven rather than assumed** | target tracking scaled every service in: `describe-services` read back desired 1 / running 1 for api, frontend AND analysis after the apply. The analysis service alone was 2 x (2 vCPU / 8 GB) idling for a feature this environment has never exercised; steady-state Fargate spend is roughly 40% lower with every autoscaling ceiling kept |
| Site | 200 |
| API errors in the thirty minutes around rollout | none |

## What this deployment carries

- **One matching run per job, across processes.** `locks.MATCHING` taken in
  `matching.run_matching` before the first vendor call; ordering pinned by
  AST; the refusal proven against the real database with a held lock
  (`tests/test_matching_lock.py`). Until today two concurrent "Run AI
  matching" clicks bought two full pipeline runs.
- **The support provider queue renders names from two IN-list statements per
  page** instead of up to fifty single-row gets, caught by the audit's
  repeatable N+1 sweep one day after the code was written.
- **The audit deliverables document**,
  `docs/architecture/ENGINEERING_AUDIT_2026-09-11.md`: seventeen
  deliverables, every claim naming its test or verification, the refusals
  with reasons, and the debt inventory.

---

# The rotated master password, and the outage it caused (2026-09-11)

**The product was DOWN and nothing had been deployed.** Sign-in returned 500;
so did `/health`. This section records what was observed, in the order it was
observed, because the misleading half is instructive and will be somebody's
first clue again.

## What the application said, and why it was the wrong clue

```
asyncpg.exceptions.InvalidAuthorizationSpecificationError: no pg_hba.conf entry
for host "10.0.11.22", user "readypick_admin", database "readypick", no encryption
```

Read as a TLS or networking fault. It is neither. asyncpg's default `prefer`
SSL mode RETRIES a refused connection without TLS, so this is the second
attempt's rejection, and the first attempt's rejection is the real one.

## What the database said

`log_connections = 1` is on in the pilot parameter group. From
`error/postgresql.log.2026-09-11-10`:

```
10.0.10.115(41530):readypick_admin@readypick:FATAL:  password authentication failed for user "readypick_admin"
10.0.10.115(41530):readypick_admin@readypick:DETAIL:  Connection matched file "/rdsdbdata/config/pg_hba.conf" line 15: "hostssl all all all md5"
10.0.10.115(41536):readypick_admin@readypick:FATAL:  no pg_hba.conf entry for host "10.0.10.115", ... no encryption
```

Two lines, one connection attempt each, the second being asyncpg's plaintext
retry of the first. The TLS rule had matched perfectly well.

## The cause, with its timestamps

| Observation | Value |
|---|---|
| RDS event | `Reset master credentials`, 2026-09-11T04:08:19Z |
| First application failure | 2026-09-11T04:28:40Z (pooled connections carried the gap) |
| Instance created | 2026-09-04T19:07:28Z, seven days earlier |
| `manage_master_user_password` | `true` -- AWS generates AND ROTATES the master password |
| `DATABASE_URL` before the repair | the master username, and a COPY of the master password |
| Password in `DATABASE_URL` vs the managed secret | **did not match** (checked programmatically, never printed) |

`infra/modules/rds` has documented the correct design since it was written:
the application uses a least-privileged role, the master exists to create that
role and to run migrations. That role had never been created.

## Restore, and the two things proven on the way

| Step | Result |
|---|---|
| `DATABASE_URL` rewritten from the AWS-managed master secret | version `b736a3a2` |
| `readypick-pilot-api` forced new deployment | rolled to one running task, PRIMARY COMPLETED |
| `GET /health` after the roll | `200 OK`, `queries=1 sql_ms=0.6` -- a real statement, not a liveness stub |
| pg_hba / auth failures in the 7 minutes after | **0** |
| `?ssl=require` added to the DSN, api rolled again | `GET /health` `200 OK`. **TLS is now proven end to end in pilot**, and the silent plaintext fallback is gone |

`ssl=require` was verified to be accepted by the installed stack before it was
deployed: SQLAlchemy 2.0.49's asyncpg dialect passes the query parameter
straight through as asyncpg's `ssl` argument.

## The durable fix, and what is proven about it

`app.scripts.provision_app_db_role` gives the existing `pickready_app` role
(migration 0001, NOLOGIN, grants maintained by every migration since) a LOGIN
and a password ReadyPick owns, makes it a NOINHERIT member of the object owner
so `alembic/env.py` can `SET ROLE` for DDL, proves the credential by opening a
second connection with it, and only then writes the DSN secret.

| Claim | Evidence |
|---|---|
| The role provisions and the credential works | Run against a local `pgvector/pgvector:pg16` at migration head: `ALTER ROLE`, the owner grant and the canonical grants all applied, and the probe connection authenticated and read through the policies |
| Rotation cannot drop the TLS parameter | `test_app_db_credential.test_the_ssl_parameter_survives_rotation` |
| Only the migration job can escalate or rewrite the DSN | `test_app_db_credential.test_only_the_migration_job_can_escalate_or_rewrite_the_dsn`, swept over every environment's Terraform |
| The write grant is `PutSecretValue` on one secret, for one service | `test_the_only_secret_anything_may_write_is_the_dsn_and_only_migrate_may` |

## What is NOT yet proven, and must not be described as if it were

- **The pilot has not been switched to the application role.** The DSN in pilot
  still carries the master credential, now with `ssl=require`. Until
  `scripts/rotate-app-db-credential.sh` has run against pilot and the services
  have been rolled, the seven-day clock is still running and the next rotation
  will take the site down again.
- **The full suite has not been run with the application role as the connection
  role.** A run was attempted and stalled during pytest collection; the stall
  reproduced and was not diagnosed. Until it is green, "the product works
  least-privileged" is a design intention and not a measurement.

---

# Vivekium reference-palette deployment (2026-10-17)

PR [#19](https://github.com/HanuLisa-Technologies-LLP/pickready/pull/19)
merged to `main` as `7dcd4e2b2e00811881df9ddae7ecf2afaf051427` after every
required CI check passed. The live site is the pilot environment in
`ap-south-2`.

| Evidence | Observed result |
|---|---|
| Backend image | `sha-7dcd4e2b2e00`, digest `sha256:f2d854850581e86d4bc00e2c4f620eaa8c4be7f7f684688449a9f8c371958075` |
| Frontend image | `sha-7dcd4e2b2e00`, digest `sha256:cfedf504b827b2f0b5c6f0b6cb8f177e0816d5f73d9dad496ea3ccb070f18846` |
| Analysis image | unchanged tag `93ebfcb`, digest `sha256:e0c6d4880b94fe3531a904042ff932ab42e3caeb19b88c03dd9b58c0fc037ec1` |
| Migration | ECS task `c9c8b6126e2b49689005490cfdbb02c8` stopped with exit 0 |
| ECS revisions | API `53`, frontend `43`, analysis `16`; all services reached stable state |
| Lambda images | `readypick-task-worker`, `readypick-jd-gen`, and `readypick-company-profile` read back `sha-7dcd4e2b2e00-fn` after update |
| Digest verification | every running API, frontend, and analysis task matched the expected digest |
| Live smoke test | `/health/live`, `/api/v1/dashboard/summary`, `/api/v1/jobs`, `/api/v1/auth/me`, and `/` returned 200; route-contract checks passed |
| Desktop browser | Vivekium title and SVG mark rendered; body background was `rgb(246, 242, 235)` (`#F6F2EB`), text and heading were `rgb(27, 43, 75)` (`#1B2B4B`), and the heading used Fraunces |
| Mobile browser | 390 by 844 viewport rendered the Vivekium header and hero with `scrollWidth=390`; no horizontal overflow |
| Browser network | document, assets, public plans, and landing telemetry succeeded; the only non-2xx responses were the expected unauthenticated session probes (`/auth/me` and `/auth/refresh`, both 401); no 5xx response was observed |

The local checkout did not contain the owner-only pilot `terraform.tfvars`, so
Terraform correctly refused to plan rather than accepting guessed environment
inputs. Because this release changed application images only, the deployed
task-definition shape was copied from the current registered revisions and
only the immutable backend or frontend image URI was replaced. No infrastructure
resource, secret, network setting, or desired count was inferred from missing
configuration.

## Rollback reference

The pre-release backend and frontend tag is `sha-e25fdf15494d`. Its backend
digest is `sha256:137e8190421ec7c13c7d079edd9e37ed900595997581a27e89fbf8d8f7bcbbd7`
and its frontend digest is
`sha256:ab7ffc715e122f47c6d8ea85cd2387c5decbb8a598d318f568a07db1a14f7aa2`.
Analysis was not changed and remains on tag `93ebfcb`. Rollback means
registering the prior immutable image URIs as new task-definition revisions,
rolling the API and frontend services, updating the three image-backed Lambda
functions to `sha-e25fdf15494d-fn`, then repeating migration, smoke, and digest
verification.

---

# Vivekium landing-content deployment (2026-10-17)

PR [#21](https://github.com/HanuLisa-Technologies-LLP/pickready/pull/21)
merged to `main` as `0a658e33cde5f93d7734a5fe71a03b0e67161afe` after all
required CI checks passed. The deployment copied the supplied home-page copy
and section order into the existing Vivekium design system; it did not add the
mock HTML's separate pages, alternate font, or client-side navigation.

| Evidence | Observed result |
|---|---|
| Backend image | `sha-0a658e33cde5`, digest `sha256:bd7354dc0b20b5931ffef9bf6416fb101a667da65f7046fece77f3876eea2eec` |
| Frontend image | `sha-0a658e33cde5`, digest `sha256:fb7869e2d305b4c25124a6db64e24c0477ec61b19688a103dd732e7d78efc020` |
| Analysis image | unchanged tag `93ebfcb`, digest `sha256:e0c6d4880b94fe3531a904042ff932ab42e3caeb19b88c03dd9b58c0fc037ec1` |
| Migration | ECS task `2ee8dfc1691546959d6ff0ebeac63388` stopped with exit 0 |
| ECS revisions | API `54`, frontend `44`, analysis `16`; services reached stable state |
| Lambda images | `readypick-task-worker`, `readypick-jd-gen`, and `readypick-company-profile` read back `sha-0a658e33cde5-fn` after update |
| Digest verification | every running API, frontend, and analysis task matched the expected immutable digest |
| Live smoke test | `/health/live`, `/api/v1/dashboard/summary`, `/api/v1/jobs`, `/api/v1/auth/me`, and `/` returned 200; route-contract checks passed |
| Desktop browser | the new "Know them before you contact them" hero, sample word-grade brief, existing palette, and Fraunces heading rendered; public plans loaded successfully and the document had no horizontal overflow |
| Mobile browser | the responsive header, stacked hero actions, and mobile copy rendered without horizontal overflow; public plans loaded successfully |
| Browser network | document, landing telemetry, public plans, and linked public routes succeeded; the only non-2xx responses were the expected unauthenticated `/auth/me` and `/auth/refresh` probes (401); no 5xx response was observed |

## Rollback reference

The pre-release backend and frontend tag is `sha-7dcd4e2b2e00`. Its backend
digest is `sha256:f2d854850581e86d4bc00e2c4f620eaa8c4be7f7f684688449a9f8c371958075`
and its frontend digest is
`sha256:cfedf504b827b2f0b5c6f0b6cb8f177e0816d5f73d9dad496ea3ccb070f18846`.
Analysis was not changed and remains on tag `93ebfcb`. Rollback means
registering the prior immutable image URIs as new task-definition revisions,
rolling the API and frontend services, updating the three image-backed Lambda
functions to `sha-7dcd4e2b2e00-fn`, then repeating migration, smoke, and digest
verification.

## 2026-10-31 public demo release A-25 (pilot)

Commit `b93dec77c274` (merge of PR #23). Images built locally for arm64 because
every CodeBuild concurrency quota in the account is 0. Backend
`sha-b93dec77c274` and `-fn` share digest
`sha256:d3a38d7566ee871c2268c7cb8fc785c21f91777f1779ebe2f7af75c528c5853f`.
Frontend (`NEXT_PUBLIC_DEMO_MODE=true`) digest
`sha256:8831d96ffb888ba90089b45db3d26e03b6f360b231e70c4a07155beda511166c`.

- Migration task `readypick-pilot-migrate:53` exited 0 (no new revision).
- API `readypick-pilot-api:57` and frontend `readypick-pilot-frontend:47`
  cloned from `:56` and `:46` with the same environment; running tasks report
  the digests above and are healthy.
- Lambdas `readypick-task-worker`, `readypick-jd-gen` and
  `readypick-company-profile` run `sha-b93dec77c274-fn`; `DEMO_MODE=true` kept.
- IAM, applied with the AWS CLI and mirrored in `infra/environments/pilot/main.tf`
  (Terraform state has not applied them): `ses:SendRawEmail` on the
  `readypick-pilot` configuration set for the API and worker roles, and the
  worker may invoke `readypick-assessment-trigger`. The policy simulator reports
  `allowed` for all three.
- A password-reset request for the owner address logged
  `security_email.sent template=security_code_password` and the task succeeded
  on attempt 1. Inbox arrival was not observed by this record.
- Acceptance script `tmp/e2e_acceptance.py` against https://readypick.ai:
  15 of 15 checks passed (persistent candidate, company and BD users across
  different passwords, job and lead persistence, demo purchase 1500 to 6000
  subunits, demo users refused on owner endpoints, owner email refused through
  demo login, BD agreement and email change refused).
- Data reset: snapshot `readypick-pilot-pre-demo-reset-20261031` taken first;
  six sample companies, their users and jobs, and the BD leads were removed.
  The provider, two BD accounts and four candidate identity rows remain.

Rollback: backend and frontend tag `sha-cdf9b73d358a` (task definitions `:56`
and `:46`); Lambdas `sha-cdf9b73d358a-fn`.

Not verified: Terraform apply (the plan carries unrelated unapplied drift),
inbox arrival of mail, and the full workflow audit (resume matching, assessment
and report generation, interviews) on the live site.
