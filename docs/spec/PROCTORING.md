# Proctoring

**Status:** current. **Reviewed:** 2026-11-01. Implemented 2026-09-02 from the owner's proctoring specification v1.0 (owner: Manju, Hanulisa Technologies LLP) and brought to the current state by the 2026-09-25 release. This file records where each rule is enforced. The seven product principles are locked; this document does not soften any of them.

Current behaviour in one paragraph: a lost camera or microphone pauses the assessment (Path P) rather than ending it; every paused second is a row the server wrote in `assessment_pauses`; the rules the candidate reads are served from the same settings the pipeline enforces; the audio rules measure a second voice and log speech during non-audio questions; every blocked paste is itemised; the session recording arrives in segments and is purged at ninety days or the job-closure purge (register P-08).

## The seven principles, and where each one is enforced

| # | Principle | Enforced by |
|---|---|---|
| P1 | **REVERSED 2026-09-22.** Assessment media IS stored; the PROCTORING module still stores none | Owner ruling, verbatim: "Media storage is required. The assessment video must be compressed and stored securely in S3, linked to the candidate assessment." The browser still infers over frames in a Web Worker and posts detections only, and the analysis service still decodes audio from bytes in memory and deletes the buffer. Assessment media is written by ONE pipeline, `services/video/`, onto ONE table, `video_recordings`, encrypted at rest, retrievable only by an authorized hiring-team member inside the tenant, and reachable by both candidate erasure and the thirty day job-closure purge. `backend/tests/test_proctoring_no_media.py` was rewritten rather than deleted and now fails the build on a media write outside that pipeline, on a bucket name or object key crossing an API boundary, and on a scorer reaching the media package. P3 and P4 are UNCHANGED. |
| P2 | No human review during a live session | There is no live proctor route, no review queue and no mid-session write path for staff. Decisions are made by `services/proctoring/ingestion.py` in the request that carries the event. |
| P3 | Proctoring never affects any score or ranking | Nothing under `services/proctoring/` is imported by any scorer, by Miti, by Siddhi, by the dashboard or by any ranking query. `backend/tests/test_proctoring_scoring_isolation.py` asserts the import graph. The report is appended to the PRISM Report as its final, informational section and read nowhere else. |
| P4 | Proctoring is mandatory | `services/proctoring/gate.require_active` runs first in both `start_conversation` and `respond`. There is no enable flag and no role bypass. The earlier optional screen-capture consent component was removed because it contradicted this principle and P1. |
| P5 | The candidate is always informed | The rules screen renders the server's `candidate_rules` (`GET /proctoring/config`, composed by `phrasing.candidate_rules` from the same numbers the pipeline applies; no fallback text when they cannot load), and the consent screen requires an explicit "I understand and agree" and the timestamp lands on `proctoring_sessions.consented_at`, which is NOT NULL. |
| P6 | Recruiter-facing language has zero internal terminology | `services/proctoring/phrasing.py` is a lookup table; `tests/test_proctoring_report.py` sweeps every generated sentence for the forbidden words. |
| P7 | Never state or imply certainty of cheating | The phrasing library describes what was detected, never what it means, and adds context sentences where a finding could be misread. |

## Architecture as built

```
browser (all inference here)
  webcam -> Web Worker: COCO-SSD (objects), MediaPipe Face Landmarker (faces,
           count, landmarks kept in a forward-compatible shape for a future
           gaze module), face-api.js (128-d identity descriptor)
  mic    -> energy VAD -> 15-second chunks, uploaded only when speech was
           present and only when the deployment has an analysis service
  lockdown (fullscreen, blocked keys and events, focus tracking)
  behavioural capture (keystroke and pointer TIMINGS, never characters)
  -> POST /api/v2/proctoring/sessions/{id}/events   (batched, JSON, no media)
  -> POST /api/v2/proctoring/sessions/{id}/heartbeat
  -> POST /api/v2/proctoring/sessions/{id}/audio     (chunk, in memory only)

backend
  services/proctoring/ingestion.py  classifies, debounces, counts warnings in
                                    Redis (authoritative), terminates
  services/proctoring/behaviour.py  evaluates typing against the candidate's
                                    own baseline at submission
  services/proctoring/audio.py      hands the chunk to the analysis service
                                    and destroys it
  services/proctoring/report.py     generates the report once, after the
                                    session ends; joins it onto the PRISM
                                    payload and the PDF as the last section

analysis-service (separate ECS service, CPU)
  POST /diarize   pyannote/speaker-diarization-3.1, speaker count only
  POST /ai-text   roberta-base-openai-detector, behind a flag, informational
```

## Every threshold lives in one place

`backend/app/core/config.py` holds every `proctoring_*` setting;
`services/proctoring/config.py` reads them once into a frozen object and
serves the browser-side subset (`CLIENT_FIELDS`) on the session response, so
the client and the server work from the same figures. No module in the
pipeline carries a literal. The specification's defaults are the settings'
defaults.

## Consequence paths (spec section 4)

`services/proctoring/catalog.py` is the vocabulary. Every event type carries
its path (A immediate termination, B the shared three-warning counter, C
logged only), its report group, whether a client may emit it, and its cooldown
or once-per-session rule. The counter is SHARED across every Path B type,
deliberately. Four identifiers were added to the specification's catalog for
rules it states in prose without naming an event: `MONITORING_INTERRUPTED`,
`INTEGRITY_CHECK_FAILED`, `IDENTITY_CHECK_MISMATCH` and `CAMERA_STREAM_FAILED`.

**Path P, the device pause (2026-09-25).** `CAMERA_PERMISSION_LOST`,
`MIC_PERMISSION_LOST`, `CAMERA_STREAM_FAILED` and `MIC_STREAM_FAILED` were
Path A and are now a fourth path, P: a loss PAUSES the assessment and its
clock. The candidate has `proctoring_device_grace_seconds` (120) to restore
the device, at most `proctoring_device_max_pauses` (2) pauses per session.
The next loss after that (`DEVICE_PAUSE_LIMIT_EXCEEDED`) or a grace EXCEEDED
(`DEVICE_RECOVERY_TIMED_OUT`; the boundary is inclusive, ties go to the
candidate) ends the session as `technical_failure`, whatever caused it. A
loss shorter than `proctoring_device_glitch_seconds` (5) pauses nothing and is
stored as `CAMERA_STREAM_INTERRUPTED` or `MIC_STREAM_INTERRUPTED` on Path C.
Downgrade only: a browser cannot earn a graver path by sending a longer
number.

- **The state is the pause row, not a flag**: "paused" is an open
  `device_loss` row in `assessment_pauses`; pauses used is a COUNT of rows.
  Concurrency is a row lock on the proctoring session (`device_pause.lock`).
- **Times are the server's**: a pause opens when the server receives the loss
  and closes when it receives the recovery; a loss reported after it ended is
  dated back by its measured duration, never by more than the grace.
- **An expired pause is settled on the next thing that arrives** (an event
  batch, a heartbeat, or the hourly `reconcile_proctoring_sessions`), which
  RETURNS the termination so it commits.
- **A heartbeat gap is recorded, never a termination**: a dead network, the
  platform's own outage and a deploy look identical from the server.
- **While paused no answer is taken** (`gate.require_answerable`, 409); the
  start route returns `SessionOut.pause` so a reload lands on the pause
  screen. Every ingest and heartbeat response carries `pause: {paused,
  message, grace_deadline_at, pauses_used, max_pauses}`.
- **The browser decides none of it**: `lib/proctoring/device-watch.ts` is the
  only place a loss becomes an event; `DEVICE_RECOVERED` is sent once, when
  every device lost in the episode is live again; the integrity episode does
  not count a device the pause owns.

## One pause record for every reason the clock stops

`assessment_pauses` (migration 0125) and
`services/assessment_conversation/pauses.py` are the one implementation, with
three reasons: `device_loss` (above), `warning` (a Path B warning that does
not end the session opens one; `POST /proctoring/sessions/{id}/warnings/ack`
closes it, capped at `assessment_warning_pause_max_seconds`, 30) and
`transcription` (a spoken answer being transcribed). The turn timer reads the
union; overlaps count once. `expires_at` is a cap every reader applies; a
close may be scheduled and only ever brought forward; the partial UNIQUE index
`uq_assessment_pauses_open` allows one open row per reason. The module imports
nothing from proctoring. This replaces the client-reported `paused_ms`.

## Audio rules

- **A second voice is flagged only when it is strong.** `/diarize` answers
  `speaker_seconds` (each speaker's own total, longest first). A chunk counts
  toward `SECOND_VOICE_DETECTED` only when the SECOND entry reaches
  `proctoring_second_voice_min_seconds` (3.0), in
  `proctoring_second_voice_consecutive_chunks` (2) consecutive chunks. An
  answer without the per-speaker list is a bad answer, not "one speaker".
- **Speaking during a question that takes no spoken answer is logged, never
  punished.** Speech of at least `proctoring_speech_min_seconds` (2.0) in a
  chunk while no spoken answer is being captured (decided by the server
  against its own `voice_answers` stamps; the browser sends no capture
  window) is `SPEECH_DURING_NON_AUDIO_QUESTION`, Path C, ONE event per run of
  speaking chunks. It is listed under Audio Monitoring every time and lifted
  into the summary from `proctoring_speech_highlight_threshold` (3)
  occurrences.

## Sustained high sound level (advisory, 2026-11-01)

`SUSTAINED_HIGH_SOUND_LEVEL` is a Path C advisory. It is deliberately NOT
Path B: it never reaches the shared warning counter, never opens a warning
pause and never ends an assessment, whatever the job's policy. Treating it as
a warning, or escalating it, is an owner decision and is not built.

- **Measured in the browser** (`lib/proctoring/sound-level.ts`, fed by the
  analyser in `audio.ts`): RMS of the microphone samples in dBFS. That is NOT a
  sound pressure level. Browsers apply automatic gain and noise suppression, so
  the same shout reads differently on two laptops. It cannot prove shouting or
  anything about the candidate, and the copy says so.
- **Rules** (all `proctoring_loud_*` settings, projected to the client in
  `CLIENT_FIELDS`): the median level over the first `calibration_seconds` is the
  room's noise floor; loud is at or above the larger of `threshold_dbfs` and
  `floor + margin_db`; it must last `sustained_seconds`; once loud it must fall
  `hysteresis_db` below the line to count as quiet; one advisory per
  `cooldown_seconds`, and one per loud stretch. A gap in the readings longer
  than half the sustained window breaks a stretch.
- **Excluded**: a reading is not judged while the candidate is dictating a
  spoken answer, while the microphone track is muted, or while it is lost
  (a lost microphone is the device pause's business). Voice input and the
  advisory coordinate through one flag owned by the voice side,
  `setVoiceRecordingActive` / `isVoiceRecordingActive` in
  `lib/assessment/voice-activity.ts` (it also fires the window event
  `vivekium:voice-recording`). The voice-answer component sets it for the
  recording phase; `audio.ts` reads it on every reading. The server applies the
  same exclusion again, below.
- **The server validates** (`ingestion._loud`): the claimed duration must reach
  `proctoring_loud_sustained_seconds`; no cooldown in force; no spoken answer
  captured around now (the same `voice_answers` probe the speech rule uses).
  A refused report is logged with its reason and not stored. Only `peak_dbfs`,
  `floor_dbfs` and `character` are kept, and the row records
  `measure: uncalibrated_microphone_level`.
- **Uncertainty is recorded**, not hidden: `character` is `voice_like` or
  `broadband`, from ONE moment's spectrum (energy share in 300 to 3400 Hz). It
  labels, it never decides. Loud music and a shout can both read either way.
- **What the candidate sees**: a dismissible status line,
  "Sustained high sound level detected; please check your environment." It holds
  no clock and needs no acknowledgement. The rules screen says it is a note
  only.
- **What the recruiter reads**: one hedged sentence under Audio Monitoring (it
  cannot tell shouting from a loud room, a fan, music or the microphone's own
  volume settings, and never earns a warning). It leads the summary only when
  nothing else was found.
- **Pending measurement**: the default numbers (-20 dBFS, 25 dB margin, 6 s,
  120 s cooldown, 10 s calibration) are reasoned starting points checked
  against synthetic level sequences (`lib/proctoring/sound-level.test.ts`), not
  tuned on real microphones, fans, dictation or auto-gain hardware. They are
  settings so they can move without a deploy once staged recordings exist.

## Every blocked paste is on its own line

`BLOCKED_ACTION_ATTEMPTED` carries the refused action in `metadata.action`
(`paste`, `copy`, `cut`, `drop`, others; a Clipboard API read reports
`paste`, a write `copy`). Every attempt is its own Path C row. The report
itemises paste, copy or cut, drag and drop, and everything else, in that
order, so a paste is never hidden inside a total; an unknown action is
counted under "another blocked action", never dropped. Answer fields and the
coding editor refuse the act and report it once, with its kind.

The server decides. The browser reports a mismatch; the server counts the
second consecutive one. The browser reports a focus loss; the server ignores
one under two seconds. Cooldowns are held in Redis so a phone on the desk
cannot burn three warnings in six seconds. Redis being unreachable answers
503 rather than silently issuing no warning.

## The recruiter's one setting (spec section 6)

`jobs.proctoring_warning_policy`, `terminate` or `continue_and_note`, default
`continue_and_note`. Set on the job page's setup review (the "Assessment
monitoring" card, with the specification's exact label, help text and two
options, on the job page's JD tab) and applied to every candidate on the
job. It moves no score.

## The report (spec section 7)

Generated once by `pickready.generate_proctoring_report` after the PRISM
report is written (completion or termination) or by the hourly
`pickready.reconcile_proctoring_sessions` sweep for abandoned sessions.
Stored on `proctoring_reports.report_content` as WORDS: counts are spelled
out, durations are approximate ("about half a minute"), and the only digits
are clock times in the date line and the activity log. That is what lets the
report travel inside `FunctionalReportOut` under the serialiser-level number
ban unchanged. The section order of the PRISM Report is now eight entries,
with `proctoring` last, written once per renderer and pinned by
`tests/test_prism_report.py` as before. `GET /proctoring/links/{id}/report`
calls `job_assessment_retention.require_readable` after the tenant check and
before loading, so a closed job answers 410 exactly as the PRISM Report does.
A termination orders the PRISM Report after commit.

**Why there is no report is stated, not implied (2026-11-01).**
`FunctionalReportOut.proctoring_state` (`{state, message}`, words only) is
built by `report.load_state` from the session and report rows, never from the
absence of events. The states: `no_session` (no monitoring session exists),
`in_progress` (the session has not ended), `generating` (ended, report not
there yet), `failed` (ended more than `proctoring_report_overdue_minutes` ago
and still no report; the hourly sweep is retrying), `partially_unavailable`
(report exists with `monitoring_was_incomplete`), `no_events` (report exists,
monitoring complete, nothing recorded) and `ready` (no message). A renderer
shows the message instead of a clean-looking blank, and above a partial report.
`failed` is a time-based reading: there is no durable failure row, so a session
with a permanently unreportable cause stays `failed` until it leaves the
sweep's window.

`pickready.reconcile_proctoring_sessions` has four jobs, in this order: close
an active session whose conversation completed (re-dispatch the report); settle
a device pause past its grace; abandon a silent session (its report is ordered
AFTER the commit with `dispatch_after_commit`, so the task cannot read the
session as still active); and re-dispatch the report of any ENDED session that
has none after the overdue interval, within the last seven days and at most two
hundred per run. `report.generate` inserts with `ON CONFLICT DO NOTHING` on the
session's unique key, so a late report, a retried delivery and a concurrent
sweep write one row. A report moves no grade or ranking (P3).

## Retention

The platform has no time-based candidate-data purge; personal data is deleted
by cascade with the candidate or the application, and the proctoring tables
cascade the same way. `proctoring_event_retention_days` is 0 by default,
meaning "the platform's policy"; a positive value enables the hourly
`pickready.purge_proctoring_events`. Choosing a number is an owner decision
and this implementation does not invent one.

The SESSION RECORDING (not a proctoring artifact: `services/video/`) follows
owner decision D4: `video_recordings.media_purge_due_at` is stamped once at
finalize, and `pickready.reconcile_assessment_recordings` purges at
`LEAST(media_purge_due_at, jobs.assessment_purge_due_at)`, ninety days
(`assessment_media_retention_days`) or the job-closure purge, whichever is
first. The S3 lifecycle rule is the backstop, not the clock. See the decision register (P-08)
and `docs/operations/INFRA_TOPOLOGY.md`.

What a reviewer sees of the recording (`GET /videos/links/{link_id}`): the four dashboard words plus `video_stage` (recording, uploaded, compressing, storing, ready, missing, failed, stalled), a sentence saying what it means and what to do, and when the pipeline last reported. `stalled` uses the repair sweep's own bounds (`video_orphan_grace_minutes`, `processing_stall_after`). The page polls while the stage is in progress and the tab is visible. Preview needs the hiring team and `view_review_screen`; download additionally re-reads the candidate's `retain_video_consent` on every request; each minted URL writes an audit row; deleted media answers 409 with the deletion sentence; no bucket or key leaves the API.

## Phone detection: what is verified and what is pending

The chain is `CameraMonitor` (sampling), the inference worker (COCO-SSD
`lite_mobilenet_v2`, served from our own origin), `DetectionRules` (confidence
over `object_confidence_threshold`, `object_consecutive_frames` in a row),
`EventQueue`, `POST .../events`, `ingestion` (Path B, shared counter,
`object_cooldown_seconds`), the warning on the candidate's screen, the report.
`lib/proctoring/phone-pipeline.test.ts` pins the client half with scripted
detections (held, brief, half-seen, flickering, failing model) and the ingest
tests pin the server half.

- **Failure is visible.** A frame the worker could not process is counted. A run
  as long as one heartbeat interval's worth of frames raises
  `SESSION_QUALITY_DEGRADED` with the note `camera_analysis_failing`; the report
  then says a phone may not have been detected for that period and sets
  `monitoring_was_incomplete`. A worker that answers frames with errors no longer
  counts as responsive, so the heartbeat reports `models: false`, which the
  server records as an `INTEGRITY_CHECK_WARNING` that the report also states.
  A model that cannot load fails the system check; there is no unmonitored start.
- **Telemetry** (numbers only, no frame, no label list): each heartbeat carries
  `detection` (`frames_processed`, `frames_failed`, `measured_fps`,
  `inference_ms_mean`, `phone_best_score`). The server logs it as
  `proctoring.detection_stats` and logs each batch as `proctoring.ingest`
  (received, stored, warned). `phone_best_score` is the highest phone confidence
  seen even when it stayed under the threshold, which is the figure needed to
  tune the threshold from staged sessions.
- **Pending measurement (not tuned).** The confidence floor (0.65) and the three
  consecutive frames are unchanged. No consented staged recordings exist yet to
  give precision and recall, and a guess either way trades missed phones against
  false warnings, which are Path B. One limit is pinned by a test rather than
  hidden: one frame under the floor restarts the run, so a phone the model
  scores 0.9, 0.9, 0.5 repeatedly is in view and never reported. Allowing a
  one-frame gap is the obvious candidate change, to be decided with measured
  figures. Comparing COCO-SSD with a YOLO model is likewise pending and, if
  done, stays in the browser worker; it is not added to the Python API.

## What is honest about the limits

Detection runs on the candidate's machine. The heartbeat, the integrity
self-check, server-side counting and a production-minified bundle raise the
effort for a determined technical candidate; they do not stop one, and the
code comments say so. A monitoring gap is reported as a gap, never as a clean
period. Audio analysis that is not configured is reported as unavailable.

## Explicitly not built (spec section 13)

Gaze tracking (landmark data is captured in a shape a future module can read),
AI-overlay detection, screen-mirroring detection, remote-desktop detection,
ID-document verification, session replay, a human review queue.
