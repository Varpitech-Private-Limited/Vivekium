# Assessment flow: invitation to PRISM Report

**Status:** current for the Tatva Assessment as built (the Vivekium simplification release, 2026-09-25). **Reviewed:** 2026-10-14. Two owner decisions of 2026-10-14 change it and are **decided, pending**, marked "Decided" below: coding without a sandbox (S-20) and the SWOT requirement and freeze (J-16). The rules and their reasons are in [../engineering/ai-runtime.md](../engineering/ai-runtime.md) and [../decisions/REGISTER.md](../decisions/REGISTER.md); this document is the sequence, the settings and the interfaces the stages call. Proctoring is [PROCTORING.md](PROCTORING.md), coding is [CODING_ASSESSMENT.md](CODING_ASSESSMENT.md), question formats are [ASSESSMENT_QUESTION_FORMATS.md](ASSESSMENT_QUESTION_FORMATS.md), job setup (the skills a candidate is assessed against) is [JOB_SETUP_FLOW.md](JOB_SETUP_FLOW.md).

## 1. The sequence

| Step | Where | Writes | Dispatches (after commit) |
|---|---|---|---|
| Apply | `POST /portal/jobs/{id}/apply` | the link, the application's six validation fields | `parse_resume`, the confirmation email |
| Invite | `select-candidates`, the `change-status` hand move, the dashboard stage control, all through `services/assessment_invitations` | the `assessment_conversations` row (the invitation), the stage | `send_assessment_invitation`, `generate_candidate_questions` |
| Questions | `workers/tasks_questions.py`, `services/assessment_questions/generate.py` | `candidate_questions` stamped with `questions_contract_digest`; coding keys in `coding_question_keys` | nothing |
| Consent and rules | `GET/POST .../consent`, `GET /proctoring/config` | the consent act | nothing |
| Start | `api/assessment_conversation.py` | `started_at`, the contract snapshot and binding, Vaada's digest line | question rewrite if the digest differs |
| Turns | `services/assessment_conversation/turns.py` | messages, `assessment_answers`, ledger rows per answer, pauses | voice transcription, coding execution |
| Completion | the last turn | `completed_at`, the charge | `run_functional_assessment`, `index_document` |
| Scoring | `functional_assessment.run_assessment` | the evaluation (one live row), the report (insert-only), `assessment_completed` | nothing |
| Reading | `api/assessment_reports.py`, `services/prism_view` | audit rows for the PDF, transcript and citations reads | nothing |

**Applying is not being invited.** Only `invite_batch` writes an
`assessment_conversations` row; a batch asks ONE credit question for its whole
cost, and the first START asks again for one report, because an invitation is
not a reservation. A closed job or unsaved skills invite nobody.

## 2. How many questions, and of which kind

`services/assessment_questions/budget.py`, pure and deterministic.

- **Budget**: one question per skill in the contract, never below the grade's
  floor. Floors: `assessment_question_floor_non_managerial` 10,
  `_managerial` 10, `_leadership` 10, `_cxo` 8. The skills store caps a bucket
  at five, so a budget is 8 to 15.
- **Mix**, counted in QUESTIONS by largest remainder, ties to prose, then
  coding, then objective: `assessment_share_prose` 0.7,
  `assessment_share_coding` 0.2, `assessment_share_objective` 0.1 (multiple
  choice and fill-in-the-blank). N=10 is 7/2/1, N=8 is 6/1/1, N=15 is 11/3/1.
  The three shares must sum to one (refused at boot otherwise).
- **Coding eligibility**, checked in order, the first failure recorded by name:
  the job's STEM verdict (`not_stem`), a computing occupation read from the
  title (`not_computing_occupation`), a sandbox that can run code
  (`code_execution_disabled`). An ineligible role's coding share joins prose.
  **Decided (S-20), not built:** the sandbox condition goes away; coding stays for computing roles only
  ([CODING_ASSESSMENT.md](CODING_ASSESSMENT.md)). Until then the sandbox is switched off, so no role is currently coding-eligible.
- **A slot the writers cannot fill becomes prose** with a recorded reason
  (`degrade_to_prose`), so the served mix differs from the plan only by a
  named degradation.
- **Nothing is pre-filled.** The resume pre-fill, the portable layer and the
  ceiling are deleted; every item is asked.
- **Questions are written against the contract.** A question row carries the
  contract digest it was written from; at the start, a mismatch deletes the
  unasked questions and answers `preparing` (a STAMPED mismatch re-dispatches
  at once, an unstamped set waits `assessment_question_redispatch_seconds`).

## 3. The start

In ONE transaction: the credit re-check for one report
(`credits.can_start_assessment`), `assessment_contract.lock_contract` under
the skills advisory lock (writes `job_skill_snapshots` version N if none, and
binds the conversation's `skill_snapshot_id` and `contract_digest`),
`log_digest("vaada", ...)`, and the `started_at` stamp. The proctoring gate
(`gate.require_active`) runs first on the start and on every answer. A
conversation whose questions are missing answers `preparing` and dispatches
after the commit; it never dispatches and then raises.

**Decided (J-16, J-19, J-20), not built:** the SWOT must be complete before assessment invitations are sent, and it freezes when the first assessment begins. Invitations are therefore refused with one server sentence when no SWOT is saved.

## 4. Turns and the clock

A TURN is every prompt the candidate is shown: a base question, a follow-up or
a re-ask. Opening one increments `turn_seq`, stamps `prompt_shown_at` once
(a reload returns the same turn and clock) and snapshots the allocation.

| Turn | Setting | Default |
|---|---|---|
| Prose (evidence-based, short answer), typed or spoken | `assessment_time_prose_seconds` | 180 |
| Multiple choice, fill-in-the-blank | `assessment_time_objective_seconds` | 60 |
| A follow-up or re-ask | `assessment_time_follow_up_seconds` | 100 |
| Coding | `assessment_time_coding_seconds` | 1200 |
| Grace for an answer in flight (inclusive) | `assessment_submit_grace_seconds` | 5 |

- **The deadline is computed from rows the server wrote**: the stamp, the
  snapshot and `assessment_pauses` (`device_loss`, `warning`,
  `transcription`; overlaps count once; `expires_at` caps each). While a pause
  is open the turn cannot expire. The client sends no time: `paused_ms` is a
  422.
- **Every answer, draft and voice call names `turn_seq`**; any other value is
  a 409 with nothing written. Only the current turn is answerable; past
  answers are the read-only `history`. There is no edit route.
- **After deadline plus grace the server submits what it holds**: a
  transcribed spoken answer, else the draft saved in time
  (`PUT /conversations/{id}/draft`), else nothing, which is an evidence gap
  (`answer_label = timed_out`, never re-asked). The same submission runs
  lazily when the candidate returns to an expired turn.
- **Follow-ups and re-asks**: Vaada (`ppi_interview.write_question`, context
  from `services/vaada_context` over the BOUND snapshot) writes a follow-up
  and its rubric in one call and persists only what is shown; a non-answer is
  challenged by `challenge_prompt`. The ledger is written per answer
  (`record_answer_evidence`), so contradiction probing reads a live ledger.
- **A conversation ends when its questions are exhausted**
  (`end_reason = prompts_exhausted`); there is no early close or extension.

## 5. Spoken answers

Prose turns only, offered only where `transcribe_enabled`. The capture is a
`voice_answers` row (one in flight per turn), the audio is stored through
`services/video/voice.py` under `voice-answers/` and refused above
`assessment_voice_max_bytes` (never truncated; `assessment_voice_max_seconds`
180). `pickready.transcribe_voice_answer` (Route.LAMBDA) runs after commit
with a `transcription` pause capped at the Transcribe budget
(`assessment_voice_transcribe_timeout_seconds`). A failure is a state: the
clock stays stopped for `assessment_voice_failure_pause_seconds` or until
acknowledged, then the candidate types. The transcript is FINAL. Only text is
kept: `assessment_conversation/voice_audio` HEAD-confirms the deletion of the
recording and Transcribe's output, and a failed delete is counted and retried.

`voice_input_available` is computed by the API from ITS OWN `TRANSCRIBE_ENABLED`, while the transcription runs in the task-worker Lambda, so BOTH runtimes must carry the flag. Every Transcribe job name starts with `transcribe.JOB_NAME_PREFIX` (`readypick-voice-`) because the Lambda's IAM grant is scoped to `transcription-job/readypick-*`; the Transcribe client and working bucket use `TRANSCRIBE_REGION` and `TRANSCRIBE_BUCKET` (ap-south-1 for a pilot in ap-south-2). The player shows: Answer by speaking (with the allow-microphone hint), Recording, Sending, Turning your answer into text, then the read-only transcript, or an actionable failure with a retry (`unavailable`) or the typing box (`failed`, after acknowledging). On a prose turn where the server cannot transcribe, the page says so and offers typing. A reload while a spoken answer is being transcribed picks it up instead of recording again. While recording, `lib/assessment/voice-activity.ts` holds a module-level flag and fires `vivekium:voice-recording`, which the proctoring audio loudness detector reads to leave dictation out of its window.

## 6. Scoring: one way, four stages

`functional_assessment.run_assessment` (the persisted task name
`pickready.run_functional_assessment`, Route.ECS) is only the orchestrator. It
takes `locks.SCORING` for the application, RETURNS on an existing report,
then asks `coding_assessment.submissions.scoring_hold`, then the credit
check, then:

1. `assessment_pipeline.evidence.load_inputs`: the transcript, the answer
   locators, the structured answers, the coding evidence, the validation
   fields; `ensure_transcript_indexed` indexes the transcript inline so
   retrieval does not race the indexer.
2. `assessment_pipeline.grading.grade`: Miti (section 7), with
   `evidence_retrieval.transcript_passages_for_skill` injected.
3. `assessment_pipeline.composition.compose`: Siddhi (section 8), with
   `evidence_retrieval.support_passages_for_statement` bound in.
4. `assessment_pipeline.persistence`: the insert-only report and its rows,
   the evaluation upsert onto `uq_evaluations_live_link`, and the move to
   `assessment_completed` through `apply_transition` in the same transaction.

A stage imports only the stages before it (`test_assessment_pipeline_direction.py`).

**Not assessed is bounded.** Before attempt `miti_not_assessed_attempts` (3),
a run with a skill not assessed writes no report: the live evaluation records
`status='not_assessed'` and `attempts`, and the hourly
`release_held_assessments` re-dispatches it. On the final attempt the report
is written with those skills "Not assessed", the overall withheld when a
Must-have is among them, `scoring_mode='miti_partial'`, routed to a person,
and `miti.not_assessed_final_report` logged at ERROR (alarm
`miti_not_assessed_final`).

## 7. Miti: the interface the grading stage calls

`miti.live.evaluate_application(session, *, job, link, conversation_id,
questions, answers, locators, structured, subject_names=(),
allow_incomplete=False, invoke=None, item_invoke=None, passages=None) ->
MitiResult`

- `questions` are the link's `candidate_questions` rows; `answers` is
  `{str(question.id): [answer text, ...]}`; `locators` is
  `assessment_pipeline.evidence.answer_records(session, link.id)`;
  `structured` is `{str(question.id): AssessmentAnswer}`; `passages` is
  `evidence_retrieval.transcript_passages_for_skill` as it is.
- **G1 first**: `load_contract_for_conversation`, `log_digest("miti", ...)`,
  `hiring.gates.contract_gate` (locked, non-empty, at least one Must-have and
  one Behavioural). A failure raises `ScorecardUnavailable` before any model
  call or ledger row. Not transient: never retried as an outage.
- **Items**: `miti/items.py` grades every contract skill. Objective formats
  read `assessment_answers.auto_score`; coding reads the sandbox evidence (70
  hidden tests, 30 review; pending, unavailable or not executed is not
  assessed; empty is unanswered) **as built; decided (S-20): the sandbox evidence is replaced by a static checker result plus AI review, scoring split unresolved (T-02)**; other prose is `answer_evaluation` (Terra,
  temperature 0) against the stored rubric (`method=general_standard` when a
  row has none); Behavioural is one judgement over every answer about the
  skill. Passages ride in their own payload field and never raise a score by
  themselves; every skill records `retrieval` (`used | degraded |
  not_requested`).
- **Statuses** (`miti/grades.py`): `graded`; `unanswered` (25, Not Matching;
  an unanswered Must-have FAILS); `not_assessed` (no score, no grade, the
  failure class). A skill with no question issued is not assessed.
- **Aggregate**: each bucket is the `1 / priority` weighted mean of its
  ASSESSED skills; the evaluators decide authenticity, dimension floors, the
  hold, confidence and a divergence review reason, and grade nothing. The
  Must-have cap is `caps.must_have_ceiling()` (71), the one number, also read
  by the ranking blend. `grades.must_have_failed` is the one predicate.
- `allow_incomplete=False` stops after the item stage when any skill is not
  assessed (`result.outcome is None`, no evaluator paid for); `True` is for
  the final attempt only.
- **`MitiResult`**: `contract`, `contract_version`, `contract_digest`;
  `skills` (one `SkillGrade` per contract skill: `skill_id`, `name`,
  `bucket`, `priority`, `status`, INTERNAL `score`, `grade` word,
  `partially_assessed`, `items`, `passages`, `retrieval`); `complete`,
  `not_assessed_skills`; `must_have_failed`; `outcome` / `aggregate`
  (`category_grades`, `overall_grade`, `overall_status`, `stated_score`,
  `must_have_cap_applied`, `confidence`, `needs_human_review`,
  `review_reasons`); `review_reasons`; `model_calls()` for provenance.
  `SkillGrade.as_dict()` is an internal working projection, never a payload.

## 8. Siddhi: the interface the composition stage calls

In this order, with the run's `ProvenanceRecorder` threaded through:

1. `siddhi.remarks.bounded_remark(session, name, evidence, *, rating,
   provenance) -> Remark` per graded skill; `unanswered_remark(name)`;
   `not_assessed_remark()`. The Overall remark uses
   `remarks.OVERALL_REMARK_WORDS`. `Remark.source` is `model | template |
   catalogue`.
2. `siddhi.inputs.evidence_by_item(questions=, competencies=, answers=,
   answer_records=)`.
3. `gap_analysis.build_gap_groups(session, dimensions, evidence_by_item, *,
   provenance)`; every entry carries `probes_source` (`model | template |
   empty_state`).
4. `siddhi.report.compose_prism(dimensions=, evidence_by_item=, gap_groups=,
   focus_summary=, overall_summary=, overall_grade=, overall_remark_source=,
   validation=, validation_points=, claim_evidence=, extra_nodes=, passages=,
   embed=support.semantic_embedder(), passage_source=...) -> ComposedPrism`.
   Uncited statements are WITHHELD (`render_collect`) and flag review;
   `gap_analysis_json` stores `{**groups, "siddhi":
   composed.siddhi_namespace()}` (trail version 2: items, locators, support
   verdicts).
5. `siddhi.quality_gate.evaluate(...)` compares the grades the document
   RENDERED with Miti's (`miti_grades_from`); a gate that cannot run fails.
6. `siddhi.ai_score.snapshot_for_report(session, link_id,
   source=yukti.pre_assessment_snapshot)` freezes the AI Match section onto
   `ai_score_json`.
7. `model_id`, `prompt_version` and `generation_provenance_json` come from the
   recorder, so a report no model wrote names no model.

Support levels (`siddhi.support`, model-written prose only): `supported`,
`weak` (including "we could not check"), `unsupported` (flags review). The
semantic check is voyage-4 cosine against `siddhi_support_similarity_min`
(0.55); a lookup in the candidate's other answers can lift `unsupported` only
to `weak`. No similarity number is stored.

## 9. The report row

`functional_skills_reports` (insert-only: `ON CONFLICT DO NOTHING`, trigger
`prism_report_is_immutable`, UPDATE revoked) carries `grade` (the CONTRACT's),
`overall_score` (Miti's `stated_score`, NULL when withheld),
`overall_status`, `must_have_failed`, `category_grades_json`,
`ai_score_json`, `gap_analysis_json`, `generation_provenance_json`,
`contract_version`, `contract_digest`, `model_id`, `prompt_version`,
`needs_human_review` and `review_findings_json`. `report_dimensions` carries
one row per contract skill with `assessment_status` (NULL score exactly when
`not_assessed`), `remark`, `remark_provenance` and `required_level` NULL. No
AI Score parameter rows are written; historic ones still render.

## 10. Reading the report

`api/assessment_reports.py` under `/api/v2/assessments` (the URL set issued
links carry is unchanged). `services/prism_view.report_out` is the one
serializer for the screen and the PDF. The closure gate
(`job_assessment_retention.require_readable`, 410) runs after the tenant
check. The PDF runs the candidate's retention consent, then
`siddhi.delivery.gate_delivery` (G4: a report routed to a person needs a
disposition that POSTDATES it; 409 `PDF_BLOCKED_REASON`), then
`delivery.prism_pdf`. The modal fetches the PDF with the session (not a bare link), saves it under the server's filename, and shows the server's own sentence for a refusal: view-only consent (403) or the G4 hold (409, `PDF_BLOCKED_REASON`). The PDF prints the serializer's `proctoring_state` message when the Proctoring Report is absent or partial. The PDF download, the transcript and the citations are
each one audited read; opening the report is not. The citations route
resolves the stored trail's locators at read time, scoped to the application,
words only. The first section is headed "AI Match"; the payload key stays
`ai_score`.
